"""
End-to-end test: run our agent against the mock arena and check that it behaves.

    python test_agent.py            # full run, about 3 minutes

What it does:
    1. starts mock_arena.py on a free port (recon 90 s, market 40 s with a 4 s fake 'closed' pause, closing 15 s)
    2. starts ../agent/agent.py in a fresh temp folder
    3. kills the agent with SIGKILL in the middle of recon, then starts it again (crash test)
    4. waits for the arena to close and the agent to exit by itself
    5. checks the results against the mock's hidden truth and prints PASS / FAIL for each check
"""
import json, os, signal, socket, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT = HERE.parent / "agent" / "agent.py"
KEY = "team_test"
START, RECON, MARKET, CLOSING = 3, 90, 40, 15
PAUSE_AT = START + RECON + 12            # 12 s into the market, like the real 11:49 glitch
KILL_AT = START + 15                     # mid-recon crash
TOTAL_SLOTS = 116


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"X-Arena-Key": KEY})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def start_agent(port, workdir, log):
    env = dict(os.environ, ARENA_URL=f"http://127.0.0.1:{port}", ARENA_KEY=KEY, PENALTY_FACTOR="0.05",
               PYTHONPATH=str(AGENT.parent), MARKET_INTERVAL="10")
    return subprocess.Popen([sys.executable, str(AGENT)], cwd=workdir, env=env, stdout=log, stderr=subprocess.STDOUT)


def main():
    port = free_port()
    workdir = tempfile.mkdtemp(prefix="arena_test_")
    print(f"mock arena port {port}, agent workdir {workdir}")
    mock = subprocess.Popen([sys.executable, str(HERE / "mock_arena.py"), "--port", str(port), "--start", str(START),
                             "--recon", str(RECON), "--market", str(MARKET), "--closing", str(CLOSING),
                             "--pause-at", str(PAUSE_AT)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    t0 = time.time()
    time.sleep(1.5)
    log = open(Path(workdir) / "console.out", "w")
    agent = start_agent(port, workdir, log)
    try:
        # crash test: hard-kill mid-recon, then restart like the organisers' instructions say
        time.sleep(max(0, KILL_AT - (time.time() - t0)))
        agent.send_signal(signal.SIGKILL)
        agent.wait()
        print(f"[{time.time() - t0:5.1f}s] agent killed mid-recon; restarting")
        agent = start_agent(port, workdir, log)

        end_by = START + RECON + MARKET + CLOSING + 60
        while agent.poll() is None and time.time() - t0 < end_by:
            time.sleep(2)
        finished_at = time.time() - t0
        exited = agent.poll() is not None
        if not exited:
            agent.kill()

        ledger = get(port, "/ledger")
        debug = get(port, "/_debug/holdings")
        holdings, dup = debug[:-1], debug[-1]["duplicate_profile_buys"]
        agent_log = (Path(workdir) / "agent.log").read_text()
        close_time = START + RECON + MARKET + CLOSING

        checks = [
            ("agent exited on its own after the real close (not during the pause)", exited and finished_at >= close_time),
            ("agent survived the mid-market 'closed' pause", "arena closed" not in agent_log.split("recon complete")[-1].split("SIGNED")[0]),
            (f"all {TOTAL_SLOTS} slots filled", ledger["signed"] == TOTAL_SLOTS),
            ("no fabricated profile signed", not any(h["fake"] for h in holdings)),
            ("no zero-point (below-bar) signing", all(h["points"] > 0 for h in holdings)),
            ("credits used within MAX_SPEND (25,000)", ledger["credits_used"] <= 25000),
            ("resumed from saved state after the crash", "resumed state" in agent_log),
            ("no profile bought twice after the restart", dup == 0),
            ("no unexpected errors in the agent log", "unexpected error" not in agent_log),
        ]
        print("\n" + "=" * 70)
        for name, ok in checks:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        print("=" * 70)
        print(f"  ledger: signed {ledger['signed']} · points {ledger['points']} · credits {ledger['credits_used']} · "
              f"score {ledger['score']} · API calls {ledger['calls']}")
        fakes = sum(1 for h in holdings if h["fake"])
        zero = sum(1 for h in holdings if h["points"] == 0)
        print(f"  holdings: {len(holdings)} people · avg points {sum(h['points'] for h in holdings) / max(1, len(holdings)):.1f} · "
              f"fakes {fakes} · zero-point {zero} · duplicate profile buys {dup}")
        print(f"  agent finished at {finished_at:.0f}s (arena closed at {close_time}s); logs in {workdir}")
        sys.exit(0 if all(ok for _, ok in checks) else 1)
    finally:
        if agent.poll() is None:
            agent.kill()
        mock.terminate()
        log.close()


if __name__ == "__main__":
    main()
