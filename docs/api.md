# The portal's JSON API (`scan --api`)

`bhulekh scan --api` talks to the same backend the portal's own Angular app uses, instead of
driving the app's UI in Chromium. Nothing about *what* is searched changes: the client hands the
identical `Row` objects to the identical matcher, thresholds and store. Only the transport differs.

Measured in the sandbox (through a slow proxy): one village = two searches ≈ 1 s sequential, and rows
were identical to the browser path on every village checked. On a normal connection with the default
concurrency of 8, expect the state-wide sweep to take well under an hour rather than ~14 h.

## Contract (recovered from `main.<hash>.js`)

Base: `https://upbhulekh.gov.in/PublicBhuApi/api` (`config.yaml: api.base_url`, or `BHULEKH_API_URL`).

| call | method | body | inner fields |
|---|---|---|---|
| `/edata` | POST | `{edata: E1({userName, passWord, userTypeId: ""})}` | — (login; returns `{jwt}`) |
| `/tehsils` | POST | `{edata: E1({districtCode})}` | `districtCode` |
| `/villages` | POST | `{edata: E1({districtCode, tehsilCode})}` | both |
| `/uniqueCoden` | POST | `{edata: E1({villageCode, name, districtCode[, fasliYear]})}` | all |

Every response is `{edata: <base64>}`; `D1(edata)` is a JSON string.

Two layers of encryption:

- **Envelope** `E1`/`D1` — AES-256-CBC, PKCS7, key `12345678901234567890123456789012`, IV
  `1234567890123456`, standard base64. Hardcoded in the app's `EncryptionMethod` service.
- **Fields** `E` — AES-128-CBC, PKCS7, key = IV = first 16 bytes (zero padded) of the *session key*,
  base64url without padding, then percent-encoded. The session key is `sha256(jwt).hex()[2:9]`
  (the app's `hashInput(jwt, 9)`).

Auth: `Authorization: Bearer <jwt>`. The JWT is minted by `/edata` from a timestamped throwaway
credential (`<random>:<seed>:DD/MM/YY HH`, seeds copied from the app) and lives ~25 minutes; the
client re-mints after 18.

`/uniqueCoden` returns the village's khatedar rows whose name starts with `name`:
`{name, father, area, unique_code, khata_number, khasra_no, status, land_type}` — the same objects
the browser's capture hook reads, decoded by the same `row_from_api`.

## Quirks the client handles

- The portal sits behind a load balancer whose nodes do not all serve every route. One answers
  `500 {"details": "No static resource api/…"}` for paths its sibling serves. Such answers are retried.
- `/districts` is not reliably deployed. The catalog's district labels are mapped to census codes by
  sweeping `/tehsils` once (2011 codes 118–188 first, widening only while labels remain unmatched)
  and matching tehsil names; the result is cached in the store's `meta` as `district_codes`.
- `/villages` rows carry `flg_chakbandi`, so villages under consolidation are known before searching.
- Old fasli bands go through the same endpoint with `fasliYear`.

## Running it

```
./bhulekh.sh scan --api --district Lucknow --limit 50
./bhulekh.sh status
./bhulekh.sh scan --api --all --strategy strategy.fanout.yaml --reset-errors
```

The catalog (districts → tehsils → villages) still comes from `bhulekh catalog`, and
`bhulekh download` still needs the browser because the extract PDF is CAPTCHA-gated.

Be a good neighbour: `api.concurrency` defaults to 8 in-flight requests. The portal is a shared public
service; the client backs off on 429 and 5xx, and there is no reason to raise the number far.
