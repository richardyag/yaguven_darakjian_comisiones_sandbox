{
    'name': 'Darakjian — Commissions',
    'summary': 'Salesperson commissions: one cliff rate per tier on the margin, paid when collected.',
    'description': """
Commissions for Darakjian Jewelers.

Business rules (set by Ara, clarified by Gabriel on 2026-07-08):

- A monthly sales volume goal in USD, set by Janel for each salesperson.
- One rate per tier applied to the TOTAL — a "cliff", NOT marginal:
    * sales < goal                      -> 3%
    * goal <= sales < 125% of goal      -> 6%
    * sales >= 125% of goal             -> 9%
- The rate applies to the MARGIN (price - cost), not to the billed amount.
- Paid when COLLECTED: a commission becomes payable once the invoice is paid.
- Computed MONTHLY, per INDIVIDUAL salesperson.

Non-invasive by design: it reads the native models (account.move, account.move.line,
account.payment, pos.order) as a read-only datasource and writes only to its own models
(yaguven.commission.*). It neither depends on nor inherits from Odoo's own commission
module (sale_commission).

Commission Source setting (19.0.1.4.0): "Invoiced sales only" (default) keeps the
original behavior — a sale with no invoice earns no commission. "All sales" also counts
POS orders that were paid but never invoiced, attributed to whoever rang them up, so a
salesperson is never penalized for a sale nobody got around to invoicing. A POS order is
counted once: as a POS order while uninvoiced, as an invoice from the moment it is.

Real COGS for invoices (19.0.1.5.0): an invoice's margin now uses the Cost of Goods Sold
actually posted on that invoice, not an estimate off today's standard_price — the two
can disagree once a product's cost changes after the sale.

Real cost for uninvoiced POS orders (19.0.1.6.0): this store's accounting only posts
COGS in one lump sum per closed POS session, with no per-order breakdown — but each
order's own stock.move already carries the exact valuation of its own movement. Margin
for an uninvoiced POS order now reads that instead of estimating off standard_price;
verified to reconcile to the cent against a real closed session's posted COGS.
""",
    'author': 'Yagüven C.G.',
    'maintainer': 'Yagüven C.G.',
    'website': 'https://github.com/Darakjian/yaguven_darakjian_comisiones',
    'category': 'Sales/Commissions',
    'version': '19.0.1.6.1',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'mail',
        'account',
        'product',
        'purchase',
        'point_of_sale',
        'stock',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/commission_config_data.xml',
        'data/commission_cron.xml',
        'data/commission_product_data.xml',
        'views/commission_config_views.xml',
        'views/commission_target_views.xml',
        'views/commission_line_views.xml',
        'views/menu.xml',
        'report/commission_target_report.xml',
    ],
    'application': True,
    'installable': True,
    'auto_install': False,
}
