import calendar
import re
from datetime import date, datetime, time

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

# Bracketed code at the start of a line name: "[DRNG.00085629] LAB GROWN ..."
CODE_RE = re.compile(r'^\s*\[([^\]]+)\]')

MONTHS = [
    ('1', 'January'), ('2', 'February'), ('3', 'March'), ('4', 'April'),
    ('5', 'May'), ('6', 'June'), ('7', 'July'), ('8', 'August'),
    ('9', 'September'), ('10', 'October'), ('11', 'November'), ('12', 'December'),
]

# The invoice payment states we treat as "collected".
COLLECTED_STATES = ('in_payment', 'paid')


class YaguvenCommissionTarget(models.Model):
    """The monthly commission goal per salesperson, and the result screen.

    This is the only thing Janel enters: salesperson, month and volume goal. Everything
    else — actual volume, tier, commission earned and collected — is materialized by the
    recompute engine reading that salesperson's native invoices for the month.
    """

    _name = 'yaguven.commission.target'
    _description = 'Darakjian — Monthly Commission Goal'
    _inherit = ['mail.thread']
    _order = 'year desc, month desc, salesperson_id'
    _rec_name = 'name'

    name = fields.Char(compute='_compute_name', store=True)

    salesperson_id = fields.Many2one(
        'res.users',
        string='Salesperson',
        required=True,
        index=True,
        domain="[('share', '=', False)]",
        context={'active_test': False},
        tracking=True,
    )
    year = fields.Integer(
        required=True,
        default=lambda self: fields.Date.context_today(self).year,
        tracking=True,
        aggregator=None,
    )
    month = fields.Selection(
        MONTHS,
        required=True,
        default=lambda self: str(fields.Date.context_today(self).month),
        tracking=True,
    )
    objective_usd = fields.Monetary(
        string='Goal (USD)',
        currency_field='currency_id',
        tracking=True,
        help='Monthly sales volume goal. Set by Janel.',
    )

    company_id = fields.Many2one(
        'res.company',
        required=True,
        index=True,
        default=lambda self: self.env.company,
    )
    currency_id = fields.Many2one(related='company_id.currency_id', store=True)
    config_id = fields.Many2one(
        'yaguven.commission.config',
        string='Tier Settings',
        ondelete='restrict',
        default=lambda self: self.env['yaguven.commission.config']._get_for_company(self.env.company),
    )

    line_ids = fields.One2many('yaguven.commission.line', 'target_id')
    line_count = fields.Integer(compute='_compute_totals', store=True)

    # --- Totals shown on the salesperson screen ---
    volume_total = fields.Monetary(
        string='Billed Volume',
        currency_field='currency_id',
        compute='_compute_totals',
        store=True,
    )
    margin_total = fields.Monetary(
        string='Total Margin',
        currency_field='currency_id',
        compute='_compute_totals',
        store=True,
    )
    tier = fields.Selection(
        [('below', 'Below Goal'), ('target', 'At Goal'), ('super', 'Above Goal')],
        string='Tier Reached',
        compute='_compute_tier',
        store=True,
    )
    pct = fields.Float(
        string='Rate Applied',
        digits=(5, 2),
        compute='_compute_tier',
        store=True,
    )
    commission_earned = fields.Monetary(
        string='Commission Earned',
        currency_field='currency_id',
        compute='_compute_commission_earned',
        store=True,
    )
    commission_collected = fields.Monetary(
        string='Commission Collected',
        currency_field='currency_id',
        compute='_compute_totals',
        store=True,
    )
    commission_pending = fields.Monetary(
        string='Pending Collection',
        currency_field='currency_id',
        compute='_compute_commission_pending',
        store=True,
    )
    last_recompute = fields.Datetime(string='Last Recompute', readonly=True)

    purchase_order_id = fields.Many2one(
        'purchase.order',
        string='Payout Purchase Order',
        readonly=True,
        copy=False,
        help='Created by "Create Purchase Order" below, for the Commission Collected '
             'amount only (what is actually payable today - not Commission Earned, '
             'which still includes margin tied to invoices the customer has not paid '
             'yet).',
    )

    _sql_constraints = [
        (
            'salesperson_period_uniq',
            'unique(salesperson_id, year, month, company_id)',
            'A goal already exists for this salesperson in this month.',
        ),
    ]

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends('salesperson_id', 'year', 'month')
    def _compute_name(self):
        month_label = dict(MONTHS)
        for rec in self:
            vendor = rec.salesperson_id.name or _('(no salesperson)')
            rec.name = '%s — %s %s' % (vendor, month_label.get(rec.month, ''), rec.year)

    @api.depends('line_ids.volume', 'line_ids.margin', 'line_ids.commission_payable')
    def _compute_totals(self):
        for rec in self:
            rec.volume_total = sum(rec.line_ids.mapped('volume'))
            rec.margin_total = sum(rec.line_ids.mapped('margin'))
            rec.commission_collected = sum(rec.line_ids.mapped('commission_payable'))
            rec.line_count = len(rec.line_ids)

    @api.depends('volume_total', 'objective_usd', 'config_id',
                 'config_id.super_threshold_pct', 'config_id.pct_below',
                 'config_id.pct_target', 'config_id.pct_super')
    def _compute_tier(self):
        for rec in self:
            config = rec.config_id or self.env['yaguven.commission.config']._get_for_company(rec.company_id)
            rec.tier, rec.pct = config._resolve_tier(rec.volume_total, rec.objective_usd)

    @api.depends('margin_total', 'pct')
    def _compute_commission_earned(self):
        for rec in self:
            rec.commission_earned = rec.margin_total * rec.pct / 100.0

    @api.depends('commission_earned', 'commission_collected')
    def _compute_commission_pending(self):
        for rec in self:
            rec.commission_pending = rec.commission_earned - rec.commission_collected

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains('year', 'month')
    def _check_period(self):
        for rec in self:
            if rec.year < 2000 or rec.year > 2100:
                raise ValidationError(_('The period year is not valid.'))

    # ------------------------------------------------------------------
    # Recompute engine
    # ------------------------------------------------------------------
    def _period_range(self):
        self.ensure_one()
        y, m = self.year, int(self.month)
        last_day = calendar.monthrange(y, m)[1]
        return date(y, m, 1), date(y, m, last_day)

    def _find_moves(self, date_from, date_to):
        """Posted invoices and credit notes for the salesperson in the period."""
        self.ensure_one()
        return self.env['account.move'].search([
            ('company_id', '=', self.company_id.id),
            ('state', '=', 'posted'),
            ('move_type', 'in', ('out_invoice', 'out_refund')),
            ('invoice_user_id', '=', self.salesperson_id.id),
            ('invoice_date', '>=', date_from),
            ('invoice_date', '<=', date_to),
        ])

    def _order_salesperson(self, order):
        """Who actually rang up a POS order: the PIN cashier if known, else the
        session owner — same preference yaguven_darakjian_pos_nav already applies when
        it sets an invoice's invoice_user_id from this same order."""
        return order.employee_id.user_id or order.user_id

    def _find_uninvoiced_pos_orders(self, date_from, date_to):
        """Paid POS orders for the salesperson in the period that were never invoiced.

        Only relevant when config_id.source_mode == 'all_sales'. Fetched company- and
        period-wide, then filtered in Python by attributed salesperson: the attribution
        can come from the PIN cashier (employee_id.user_id), which no plain ORM domain
        on pos.order can express directly.

        date_order is a Datetime field; comparing it against bare date objects lets Odoo
        widen the bounds by the acting user's timezone (UTC for a cron, whatever the
        logged-in user has otherwise) — not deterministic, and it can silently drop
        orders from the first or last hours of the period. Built as explicit UTC
        datetimes instead, matching how date_order is actually stored, so the period
        boundary never shifts depending on who triggers the recompute.
        """
        self.ensure_one()
        datetime_from = datetime.combine(date_from, time.min)
        datetime_to = datetime.combine(date_to, time.max)
        candidates = self.env['pos.order'].search([
            ('company_id', '=', self.company_id.id),
            ('state', 'in', ('paid', 'done', 'invoiced')),
            ('account_move', '=', False),
            ('date_order', '>=', datetime_from),
            ('date_order', '<=', datetime_to),
        ])
        return candidates.filtered(lambda o: self._order_salesperson(o) == self.salesperson_id)

    def _product_from_line_name(self, name, cache=None):
        """Recover the product from the [code] embedded in the line name.

        Migrated invoices carry product lines with NO product_id linked, but with the
        default_code in brackets inside the text ([DRNG.00085629] ...). Matched on the
        exact default_code first and, failing that, on a normalized suffix.
        Returns a recordset, empty when it cannot be resolved.
        """
        Product = self.env['product.product']
        if not name:
            return Product.browse()
        m = CODE_RE.match(name)
        if not m:
            return Product.browse()
        code = m.group(1).strip()
        if cache is not None and code in cache:
            return cache[code]
        prod = Product.search([('default_code', '=', code)], limit=1)
        if not prod and '.' in code:
            # The suffix after the last dot, ignoring leading zeros.
            suffix = code.split('.')[-1]
            prod = Product.search([('default_code', '=like', '%.' + suffix)], limit=1)
        if cache is not None:
            cache[code] = prod
        return prod

    def _move_cost_from_cogs(self, move):
        """Real posted Cost of Goods Sold for this move, read straight from accounting.

        This store posts the COGS entry on the SAME move as the invoice (one expense
        line per product line, in the same currency/sign as the invoice itself) rather
        than on a separate delivery-triggered valuation move — confirmed by inspecting
        real invoices and credit notes: a return's COGS line already comes back negative
        on its own, no re-signing needed. Returns None when the move has no such line at
        all (e.g. older migrated invoices with no COGS ever posted), so the caller can
        fall back to the standard_price estimate instead of silently reporting 0 cost.
        """
        cogs_lines = move.line_ids.filtered(
            lambda l: l.account_id.account_type == 'expense_direct_cost'
        )
        if not cogs_lines:
            return None
        return sum(cogs_lines.mapped('balance'))

    def _move_cost_from_standard_price(self, move, code_cache=None):
        """Fallback estimate: product standard_price x quantity, today's cost — used
        only when the move has no real COGS line to read (see _move_cost_from_cogs)."""
        company = self.company_id
        sign = -1.0 if move.move_type == 'out_refund' else 1.0
        cost_total = 0.0
        for line in move.invoice_line_ids:
            if line.display_type and line.display_type != 'product':
                continue
            product = line.product_id or self._product_from_line_name(line.name, code_cache)
            if product:
                cost_total += product.with_company(company).standard_price * line.quantity
        return sign * cost_total

    def _move_snapshot(self, move, code_cache=None):
        """Snapshot of the invoice's net volume and cost, in company currency.

        - Volume = the net of EVERY sale line (display_type='product'), linked to a
          product or not: this is the sales volume that decides the tier.
        - Cost = the real Cost of Goods Sold posted on this move, when there is one;
          the product's standard_price x quantity only as a fallback for moves with no
          COGS line at all (see _move_cost_from_cogs).
        - Credit notes (out_refund) come in with a negative sign.
        """
        self.ensure_one()
        company = self.company_id
        sign = -1.0 if move.move_type == 'out_refund' else 1.0
        net_move_ccy = 0.0
        for line in move.invoice_line_ids:
            if line.display_type and line.display_type != 'product':
                continue
            net_move_ccy += line.price_subtotal
        cost_total = self._move_cost_from_cogs(move)
        if cost_total is None:
            cost_total = self._move_cost_from_standard_price(move, code_cache)
        # Net converted to company currency (USD); the cost is already in it.
        if move.currency_id and move.currency_id != company.currency_id:
            rate_date = move.invoice_date or fields.Date.context_today(self)
            net_company = move.currency_id._convert(
                net_move_ccy, company.currency_id, company, rate_date)
        else:
            net_company = net_move_ccy
        return {
            'volume': sign * net_company,
            'cost_total': cost_total,
            'is_collected': move.payment_state in COLLECTED_STATES,
        }

    def _pos_order_cost_from_stock(self, order):
        """Real cost of this uninvoiced POS order's stock movement — not an estimate.

        This store's accounting only posts COGS in one lump sum per closed POS session
        (every uninvoiced order in it summed together), with no stock.valuation.layer
        model to read a per-order figure from either. But ``stock.move.value`` on each
        order's own picking already holds the exact valuation of that specific movement
        — verified against a real closed session's posted COGS line, to the cent. The
        move carries no sign of its own, so direction is read off the Customer location:
        stock leaving TO the customer is a cost (a sale), stock coming back FROM the
        customer reverses it (a return) — the same convention this store's own inventory
        valuation uses.
        """
        total = 0.0
        moves = order.picking_ids.move_ids.filtered(lambda m: m.state == 'done')
        for move in moves:
            if move.location_dest_id.usage == 'customer':
                total += move.value
            elif move.location_id.usage == 'customer':
                total -= move.value
        return total

    def _pos_order_snapshot(self, order):
        """Snapshot of an uninvoiced POS order's net volume and cost, company currency.

        Volume is the net of every product line. Cost is the order's real stock
        movement value (see ``_pos_order_cost_from_stock``) — falls back to the
        product's standard_price estimate only if the order has no done stock move at
        all to read (e.g. a non-stockable line). A POS order only reaches 'paid'/'done'
        once the register has collected full payment, so it is always counted as
        collected.

        ``price_subtotal`` is read as a magnitude and re-signed from ``qty`` rather than
        trusted as-is: a refund line (negative qty) can carry a stale positive
        price_subtotal if it was never recomputed after being written directly (e.g. a
        refund created by anything other than the normal POS refund flow), which would
        otherwise make a return add to volume instead of subtracting from it.
        """
        self.ensure_one()
        company = self.company_id
        net = 0.0
        for line in order.lines:
            sign = -1.0 if line.qty < 0 else 1.0
            net += abs(line.price_subtotal) * sign
        if order.picking_ids.move_ids.filtered(lambda m: m.state == 'done'):
            cost = self._pos_order_cost_from_stock(order)
        else:
            cost = 0.0
            for line in order.lines:
                if line.product_id:
                    cost += line.product_id.with_company(company).standard_price * line.qty
        if order.currency_id and order.currency_id != company.currency_id:
            rate_date = order.date_order.date() if order.date_order else fields.Date.context_today(self)
            net_company = order.currency_id._convert(net, company.currency_id, company, rate_date)
        else:
            net_company = net
        return {
            'volume': net_company,
            'cost_total': cost,
            'is_collected': True,
        }

    def _recompute_one(self):
        self.ensure_one()
        config = self.config_id or self.env['yaguven.commission.config']._get_for_company(self.company_id)
        date_from, date_to = self._period_range()
        moves = self._find_moves(date_from, date_to)
        pos_orders = self.env['pos.order']
        if config.source_mode == 'all_sales':
            pos_orders = self._find_uninvoiced_pos_orders(date_from, date_to)

        existing_by_move = {line.move_id.id: line for line in self.line_ids if line.move_id}
        existing_by_order = {line.pos_order_id.id: line for line in self.line_ids if line.pos_order_id}
        seen_moves = set()
        seen_orders = set()
        code_cache = {}
        for move in moves:
            seen_moves.add(move.id)
            snap = self._move_snapshot(move, code_cache)
            line = existing_by_move.get(move.id)
            if line:
                # Volume and cost stay frozen; only the collection state is refreshed.
                line.is_collected = snap['is_collected']
            else:
                self.env['yaguven.commission.line'].create({
                    'target_id': self.id,
                    'move_id': move.id,
                    'volume': snap['volume'],
                    'cost_total': snap['cost_total'],
                    'is_collected': snap['is_collected'],
                })
        for order in pos_orders:
            seen_orders.add(order.id)
            snap = self._pos_order_snapshot(order)
            line = existing_by_order.get(order.id)
            if not line:
                self.env['yaguven.commission.line'].create({
                    'target_id': self.id,
                    'pos_order_id': order.id,
                    'volume': snap['volume'],
                    'cost_total': snap['cost_total'],
                    'is_collected': snap['is_collected'],
                })
        # Dropped: sales that stopped qualifying (unposted/cancelled/reassigned moves,
        # or POS orders that got invoiced meanwhile — picked up as a move instead).
        stale = self.line_ids.filtered(
            lambda l: (l.move_id and l.move_id.id not in seen_moves)
            or (l.pos_order_id and l.pos_order_id.id not in seen_orders)
        )
        stale.unlink()

        # Tier from the month volume, one single rate applied to every line (cliff).
        volume_total = sum(self.line_ids.mapped('volume'))
        _tier, pct = config._resolve_tier(volume_total, self.objective_usd)
        if self.line_ids:
            self.line_ids.write({'pct_applied': pct})
        self.last_recompute = fields.Datetime.now()

    def _recompute(self):
        for target in self:
            target._recompute_one()
        return True

    def action_recompute(self):
        self.with_context(skip_commission_recompute=True)._recompute()
        return True

    # ------------------------------------------------------------------
    # Print / payout
    # ------------------------------------------------------------------
    def action_print_report(self):
        return self.env.ref(
            'yaguven_darakjian_comisiones.action_report_commission_target'
        ).report_action(self)

    def _get_commission_payout_product(self):
        return self.env.ref('yaguven_darakjian_comisiones.product_commission_payout')

    def action_create_purchase_order(self):
        """Create (once) a Purchase Order against the salesperson for Commission
        Collected - the portion of the commission that is actually payable today,
        because the underlying invoice has been collected. Commission Earned is
        deliberately NOT used here: it still includes margin tied to invoices the
        customer has not paid yet, which is not owed out of pocket until it is.

        Idempotent: a second click on an already-paid-out target reopens the existing
        PO instead of creating a duplicate.
        """
        self.ensure_one()
        if self.purchase_order_id:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'purchase.order',
                'view_mode': 'form',
                'res_id': self.purchase_order_id.id,
            }
        if not self.salesperson_id.partner_id:
            raise UserError(_(
                'The salesperson "%s" has no related contact to use as the Purchase '
                'Order vendor.'
            ) % self.salesperson_id.name)
        if self.commission_collected <= 0:
            raise UserError(_(
                'Nothing is payable yet for this period: Commission Collected is %s. '
                'Pending Collection becomes payable once the customer invoices are paid.'
            ) % self.commission_collected)

        product = self._get_commission_payout_product()
        order = self.env['purchase.order'].create({
            'partner_id': self.salesperson_id.partner_id.id,
            'currency_id': self.currency_id.id,
            'company_id': self.company_id.id,
            'origin': self.name,
            'order_line': [(0, 0, {
                'product_id': product.id,
                'name': _('Sales commission payout — %s') % self.name,
                'product_qty': 1,
                'product_uom_id': product.uom_id.id,
                'price_unit': self.commission_collected,
            })],
        })
        self.purchase_order_id = order
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'purchase.order',
            'view_mode': 'form',
            'res_id': order.id,
        }

    # ------------------------------------------------------------------
    # Automatic triggers
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records.with_context(skip_commission_recompute=True)._recompute()
        return records

    def write(self, vals):
        res = super().write(vals)
        if not self.env.context.get('skip_commission_recompute'):
            triggers = {'objective_usd', 'salesperson_id', 'year', 'month', 'company_id', 'config_id'}
            if triggers & set(vals):
                self.with_context(skip_commission_recompute=True)._recompute()
        return res

    @api.model
    def _cron_recompute(self):
        """Refresca el mes en curso y el anterior (cobros que van entrando)."""
        today = fields.Date.context_today(self)
        prev = today.replace(day=1) - date.resolution
        targets = self.search(
            ['|',
             '&', ('year', '=', today.year), ('month', '=', str(today.month)),
             '&', ('year', '=', prev.year), ('month', '=', str(prev.month))]
        )
        targets.with_context(skip_commission_recompute=True)._recompute()
        return True
