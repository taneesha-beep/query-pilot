# Recorded provider bodies

Test material, not documentation. Each file is what a provider returned on **2026-09-06**,
lifted out of [`docs/provider-check.json`](../../docs/provider-check.json) so that the
client's error handling is written against bodies that were observed rather than imagined.

| File | Provenance |
|---|---|
| `google_daily_429.json` | Verbatim. `gemini-3.8-flash` refusing on `GenerateRequestsPerDayPerProjectPerModel-FreeTier` = 20 — the wall that clears at midnight Pacific. |
| `google_minute_429.json` | **Derived, not verbatim.** The 0.4 probe truncated throttle messages at 300 characters, so no per-minute body survives whole. This is the daily body above with the quotaId, quotaValue, metric and retryDelay the probe *did* record for `gemini-3.5-flash-lite` substituted in. Both halves were observed; only their combination is reconstructed. |
| `google_retired_404.json` | Verbatim. `gemini-2.5-flash`, listed by the models endpoint and retired. |
| `groq_rpm_429.txt` | Verbatim, and truncated at 300 characters by the probe that recorded it. Groq names its ceiling in prose, so a truncated body is still enough to read `RPM: Limit 30` and `try again in 2s` from. |

The truncation that cost us the per-minute body is why the client keeps error bodies
whole: a quota a provider names once is worth more than the bytes it takes to store.
