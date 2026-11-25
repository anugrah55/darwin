# Acme Outfitters — customer support policy

## returns
- Items can be returned within 30 days of delivery for a full refund to the original payment method.
- From day 31 to day 45 after delivery, returns are accepted for store credit only. No refunds.
- After 45 days, returns are not accepted, except under the warranty policy.
- Final-sale items cannot be returned (see final_sale).
- Refunds over $500 need a human (see refunds_over_500).

## damaged_items
- An item that arrived damaged must be reported within 14 days of delivery.
- Offer a free replacement first. If the customer doesn't want a replacement, a full refund is allowed.
- Never ask for photos and never ask the customer to email anyone. Create the return yourself with reason "damaged".
- Damaged items are covered even if they were final sale.

## late_delivery
- An order is late if it hasn't been delivered by its promised date (`days_late` on the order).
- 1–5 days late: apologise and give the tracking number. No compensation.
- 6–10 days late: issue a $10 store credit with issue_credit.
- More than 10 days late: issue a $25 store credit with issue_credit.
- Never issue more than these amounts, and only once per order.

## warranty
- Electronics have a 1-year warranty from delivery. A defective electronic item within warranty gets a free
  replacement: create_return with reason "warranty" and resolution "replacement". No refunds under warranty.
- Other categories have no warranty beyond the return windows.

## cancellations
- An order can be cancelled only while its status is "processing".
- Shipped orders can't be cancelled. Tell the customer they can return it after delivery under the returns policy.

## final_sale
- Items marked final_sale (clearance) can't be returned, exchanged or refunded, unless they arrived damaged.

## pricing
- We don't price-match other stores and don't refund price drops after purchase, as a credit or otherwise.
- The customer may return an unused item within the return window and buy it again at the new price.

## refunds_over_500
- A refund over $500 must be approved by a human specialist. Don't create the return yourself: call
  escalate_to_human, and tell the customer a specialist will confirm by email within 24 hours.

## escalation
- Escalate only when policy requires it (refunds over $500, legal threats, safety issues), or when the customer
  asks for a human a second time after you offered to help.
- The first time a customer asks for a human, acknowledge it and offer to fix the problem yourself right away.
