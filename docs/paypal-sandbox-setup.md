# PayPal Sandbox setup

PACT talks to the PayPal Sandbox only. A payment goes through two different PayPal identities, and
mixing them up is the usual reason a checkout fails on PayPal's page even though PACT is configured
correctly.

## What you need

| Thing | What it is for | Where it is used |
|---|---|---|
| Sandbox **business** account | Receives the money. It owns the REST app. | Nowhere directly; PayPal ties it to the app. |
| REST app (client id + secret) | Lets PACT create and capture orders for that business account. | `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` in `.env` |
| Sandbox **personal** account | The buyer. Pays on PayPal's checkout page. | Typed into PayPal's page by the human, never into PACT. |

All three live at <https://developer.paypal.com>. None of them is a real PayPal account, and no real
money moves.

## The rule that matters

On PayPal's checkout page, log in with the **personal** Sandbox account.

Do not use:

- the business Sandbox account (it is the one being paid; an account cannot pay itself);
- your developer-dashboard login;
- a real PayPal account.

Any of these makes the login fail, or makes PayPal show a card form instead of a login (see
"The card form" below).

## Country pair

The business and personal accounts must be in countries that are allowed to pay each other.

Sandbox creates one default business and one default personal account in the country your developer
account is registered in. In some countries PayPal does not allow domestic payments; India is one.
There, the two default accounts are both Indian and cannot pay each other, so every checkout fails no
matter what PACT does.

The fix is to create your own pair in a country that can transact, for example both in the United
States:

1. developer.paypal.com → **Testing Tools** → **Sandbox Accounts** → **Create account**.
2. Create a **Business** account, country United States.
3. Create a **Personal** account, country United States. Give it a balance (the default is fine).

## Create the REST app under the right business account

The app decides who gets paid. An app created under the default business account pays the default
business account, even if you later create a better one.

1. developer.paypal.com → **Apps & Credentials**, with the **Sandbox** toggle selected.
2. **Create App**. Choose type Merchant, and in **Sandbox Business Account** pick the business account
   you created above.
3. Copy the client id and secret into `.env` as `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`.
4. Set `PACT_PAYMENT_PROVIDER=paypal`, and keep `PACT_CURRENCY=USD` unless you know the business account
   accepts another currency.

If you change the pair later, create a new app under the new business account and replace both values.

## Find the buyer's email and password

developer.paypal.com → **Testing Tools** → **Sandbox Accounts** → the `...` menu on the personal
account → **View/edit account**. The email and the system-generated password are shown there. You can
set a password you will remember on the same screen.

These are what the human types on PayPal's checkout page.

## Check it with `pact doctor`

```
uv run pact doctor
```

In PayPal mode the doctor:

1. gets an OAuth token ("PayPal Sandbox credentials");
2. creates one small test order in `PACT_CURRENCY` ("PayPal Sandbox order"). Nobody approves it, so
   nothing is paid and PayPal expires it on its own.

When the order check passes it prints the order status and, when PayPal returns it, the business
account that will receive the money. Compare that with the account you intend to be paid, and make
sure the account you log in with at checkout is a different, personal one.

When it fails it prints PayPal's code and what to do. Common ones:

| Code | Meaning | Fix |
|---|---|---|
| `CURRENCY_NOT_SUPPORTED` | The business account cannot accept `PACT_CURRENCY`. | Set `PACT_CURRENCY=USD`. |
| `PAYEE_ACCOUNT_RESTRICTED` | The business account is restricted. | Create a new business account and a new app under it. |
| `PAYPAL_AUTH_FAILED` | Wrong client id or secret, or a live (non-Sandbox) app. | Copy the Sandbox app's values again. |
| "no clear answer from PayPal" | Timeout or PayPal server error. | Run the doctor again. |

`uv run python examples/paypal_sandbox_check.py` does the same order check by hand and also prints the
buyer approval URL, so you can try the checkout page without going through an agent.

A passing doctor proves the app and the business account work. It cannot prove the buyer side: only a
real login on PayPal's page with the personal account does that.

## The card form

If PayPal's page shows a "pay with debit or credit card" form, that is **guest checkout**: paying
without a PayPal account. You see it when you pick it, or when PayPal does not recognise the email you
entered as a Sandbox personal account.

In Sandbox it is unreliable and is not the way to test PACT:

- It depends on the business account's settings and on the buyer and merchant countries. Many
  Sandbox business accounts are not enabled for it.
- Test card numbers are often rejected on this form even when they are valid elsewhere.
- Direct card orders against a business account that is not set up for card processing are refused
  with `PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING`.

Log in with the personal Sandbox account instead. If you land on the card form, go back and enter the
personal account's email exactly as shown in the dashboard.

`pact doctor` does not test whether the business account can take cards. Doing so needs a card order
call that PACT's PayPal adapter does not make, so this limit is documented here rather than checked.

## What PACT stores

PACT stores the REST app's client id and secret, in `.env`, and nothing else from PayPal's side of the
login. It never asks for, receives, logs or stores the buyer's email or password; the buyer types them
only into PayPal's own page. The doctor and the example script do not print the client secret or
access tokens.

## Checklist

- [ ] Business and personal Sandbox accounts exist, in a country pair that can transact (for example both US).
- [ ] The REST app was created under that business account, and its Sandbox client id and secret are in `.env`.
- [ ] `PACT_PAYMENT_PROVIDER=paypal`, `PACT_CURRENCY=USD`.
- [ ] `uv run pact doctor` passes "PayPal Sandbox credentials" and "PayPal Sandbox order", and the receiving account it prints is the business account you expect.
- [ ] You know the personal account's email and password, and you use them, and only them, on PayPal's page.
