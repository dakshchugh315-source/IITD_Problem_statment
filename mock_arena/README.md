# Mock Battle Arena

A local copy of the Battle Arena API, for testing the agent without spending real credits.
Standard library only.

## Run the mock by itself
```
python3 mock_arena.py --port 8765 --recon 90 --market 40 --closing 15
```
Then point any agent at it: `ARENA_URL=http://127.0.0.1:8765 ARENA_KEY=anything python3 ../agent/agent.py`

| Option | Default | Meaning |
|---|---|---|
| `--pool` | 20000 | Candidates generated |
| `--seed` | 7 | Same seed = same pool |
| `--start / --recon / --market / --closing` | 3 / 90 / 40 / 15 | Phase lengths in seconds |
| `--pause-at`, `--pause-len` | off, 4 | Fake a 'closed' pause mid-market (the 11:49 glitch) |
| `--rivals` | 40 | Rival signings per second in the first 10 s of the market |
| `--penalty` | 0.05 | Penalty factor used in the ledger score |

## What it simulates
- The same 10 endpoints, credit costs, phases (`closed → recon → market → closing → closed`) and response shapes as the live arena.
- The 10 real requisitions.
- Messy profiles: `Postgres | k8s | REST`, `67 months`, `25.9 LPA` or `2160000`, `Immediate` / `2 months`, assessments as `72`, `72/100`, `0.72`, `7.2/10` or empty.
- 6% fabricated profiles: self-score 85–99, verified 25–55, `identity_verified: false`.
- 2% duplicate people under a second id.
- Rate limit 10 req/s per key (burst 30), credit exhaustion, and wrong-phase 409s.
- Rival teams that grab high self-score candidates once the market opens.
- `/reason` answers in the same fenced-JSON shape as the real model.
- Hidden points per signing, following the organisers' points note (fakes and below-bar signings score 0).

`GET /_debug/holdings` (mock only) reveals the truth about every candidate a key holds.

## Run the end-to-end test
```
python3 test_agent.py      # about 3 minutes
```
The test:
1. Starts the mock and the agent.
2. Kills the agent mid-recon (a crash) and restarts it.
3. Fakes a 'closed' pause during the market.
4. Waits for the real close, then prints PASS/FAIL for 9 checks: clean exit, pause survived, all slots filled, no fakes, no zero-point signings, spend cap, resume, no double buying, and no errors.
