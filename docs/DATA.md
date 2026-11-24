# Data notes

Facts about `data/data.db`, checked with read-only queries. `data/schema.sql` is the source of truth for structure; the brief's description is not.

## Tables

| Table | Rows | Notes |
|---|---|---|
| `transactions` | 1,159,966 | `date` from 2019-01-01 to 2019-10-31, stored as text `YYYY-MM-DD HH:MM:SS` |
| `fraud_labels` | 777,339 | One row per labelled transaction; `is_fraud` is 0 or 1 |
| `merchant_locations` | 70,539 | `city`, `state`, `zip` per merchant location. `state` is a two-letter US code (69,181 rows, all with a `zip`), a country name outside the US (1,253 rows, no `zip`), or `''` for the 105 online locations (`city = 'ONLINE'`, no `zip`) |
| `merchants` | 27,516 | `name`, `mcc` |
| `cards` | 3,437 | Belongs to a user. `card_on_dark_web` is 0 for every card |
| `users` | 1,206 | Demographics, income, debt, credit score |
| `mcc_codes` | 109 | Merchant category code → description |

## Relationships

```
users 1─* cards 1─* transactions *─1 merchants *─1 mcc_codes
                     transactions *─1 merchant_locations *─1 merchants
                     transactions 1─0..1 fraud_labels   (transactions.id = fraud_labels.transaction_id)
```

## Where the data differs from the brief

| Brief | Actual |
|---|---|
| Five tables | Seven: also `merchants` and `merchant_locations` |
| Example rule uses `amount > 1000` | Column is `amount_usd_cents` (integer cents), so $1,000 is `100000` |
| Example rule uses `card_type = 'debit'` | Values are `'Credit'`, `'Debit'`, `'Debit (Prepaid)'`; comparison is case-sensitive |
| `fraud_labels`: "labels for transactions" | Only 777,339 of 1,159,966 transactions (about 67%) have a label |
| (not stated) | `fraud_labels.transaction_id` is TEXT while `transactions.id` is INTEGER |

The brief's example rule, run as written, fails on the column name; with the name fixed it would still match nothing on `card_type`.

## Things that affect correctness

- **Partial labels.** A transaction without a label is unknown, not legitimate. Fraud rates and rule metrics must use labelled transactions only (inner join) and say so. A `LEFT JOIN` with `is_fraud` treated as 0 when missing understates fraud.
- **Class imbalance.** 1,360 of the 777,339 labels are fraud (about 0.17%); 775,979 are not. Accuracy is meaningless here; use precision, recall and counts.
- **Type mismatch on the label join.** `f.transaction_id = t.id` gives the right rows because SQLite converts the text side for the comparison, and every label matches exactly one transaction (777,339 joined rows). But the integer cannot be looked up in the text index on `fraud_labels`, so the uncast form is only fast as an inner join (0.65 s; the planner scans the labels and finds each transaction by primary key). As a `LEFT JOIN` from `transactions`, or inside `EXISTS`, it compares every transaction with every label: both were interrupted after 8 seconds without finishing. Use `f.transaction_id = CAST(t.id AS TEXT)`: it uses the index in every shape (inner join 0.65 s, `LEFT JOIN` 0.19 s) and matches the same rows, because every label id is the canonical text of an integer. See [D-10](DECISIONS.md#d-10-the-schema-context-prescribes-one-form-of-the-label-join).
- **Money is in cents** everywhere (`amount_usd_cents`, `credit_limit_usd_cents`, `yearly_income_usd_cents`, …). Divide by 100 for display only.
- **Negative amounts.** 56,900 transactions have a negative `amount_usd_cents` (minimum −50,000). Their meaning (likely refunds) is not documented.
- **`errors` is usually NULL.** 1,141,312 transactions have no error; otherwise one of seven fixed values (see `schema.sql`).
- **Booleans are 0/1 integers** (`has_chip`, `card_on_dark_web`, `is_fraud`). Dates are ISO text, so SQLite date functions and string comparison both work: `transactions.date` is always `YYYY-MM-DD HH:MM:SS` (the seconds are always `00`), and `birth_date`, `expires` and `acct_open_date` are `YYYY-MM-DD`.
- **Fixed value sets** are declared as `CHECK` constraints in `schema.sql` (`transaction_type`, `errors`, `card_brand`, `card_type`, `gender`). Every declared value occurs in the data, so the schema context lists the distinct values it reads from the database.
- **Online transactions.** All 139,987 `'Online Transaction'` rows point at a location with `city = 'ONLINE'`; so do 1,084 `'Chip Transaction'` rows.
- **No secondary indexes.** Only primary keys are indexed, so filters and joins on other columns scan 1.16M rows.

## Access

- Always open it read-only: `sqlite3 'file:data/data.db?mode=ro'`. In the application only `db.py` opens it ([D-06](DECISIONS.md#d-06-read-only-is-enforced-in-three-engine-level-layers)).
- The database is in WAL mode, and that shows even when reading. Checked on a copy, with SQLite 3.51:
  - Connecting with `mode=ro` creates nothing. The first query creates `data.db-shm` (32,768 bytes) and an empty `data.db-wal` when they are absent, and closing the connection leaves them there. Both are git-ignored. `data.db` itself is byte-identical afterwards (same SHA-256), also after rejected write attempts.
  - If those files are absent and the directory is not writable, a `mode=ro` connection cannot read the database ("attempt to write a readonly database").
  - `mode=ro&immutable=1` reads without creating either file, but it ignores the WAL and takes no locks, so it is only correct while nothing writes to the database. The application does not use it.
  - `mode=ro` alone still lets a connection `ATTACH` and write another file, or create temp tables; `db.py` blocks those with an authorizer.
- The test suite removes any sidecar file it caused to be created and checks that `data.db` is unchanged.
