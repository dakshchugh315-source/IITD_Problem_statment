"""
Battle Arena agent - Innov8 4.0 finale.

    export ARENA_URL=https://...   ARENA_KEY=arena_xxx   PENALTY_FACTOR=0.05
    python agent.py

Strategy in one breath:
    recon    sweep search pages (1 credit / 100 summaries), rank locally by normalised skill fit,
             batch-buy full profiles for the best (1.2 credits each), filter on the real bar
             (assessment, notice, CTC), pay for /assess only on top picks that look suspicious,
             and build a ranked sign list per requisition with backups.
    market   the moment offers open, sign the best picks across all requisitions in parallel,
             fall through to backups on rejection, then top up open slots every few minutes.
    closing  offers cost double: only sign where expected points beat the doubled cost.
Every spend is gated on  expected_points_gained > credits x PENALTY_FACTOR.
State is written to disk after each step, so a crash or redeploy never pays twice.
"""
import json, os, sys, threading, time, traceback
from concurrent.futures import ThreadPoolExecutor

from arena_client import Arena, Exhausted, WrongPhase
from normalize import pick, norm_role
from scoring import (Requisition, candidate_id, summary_score, evaluate, risk_flags, identity_keys,
                     self_assessment)
from learning import AssessmentModel, NoteScorer, verdict_from_assess

# ------------------------------------------------------------------ knobs (all env-overridable)
def knob(name, default):
    return type(default)(os.environ.get(name, default))

PENALTY_FACTOR = knob("PENALTY_FACTOR", 0.05)
SEARCH_PAGES_PER_ROLE = knob("SEARCH_PAGES_PER_ROLE", 120)   # x100 summaries, 1 credit each
PROFILE_DEPTH = knob("PROFILE_DEPTH", 12)                   # profiles bought per headcount slot, first pass
PROFILE_CAP = knob("PROFILE_CAP", 40)                       # never buy more than this x headcount per req
BACKUP_FACTOR = knob("BACKUP_FACTOR", 3)                    # want this many eligible per slot before signing
ASSESS_BUDGET = knob("ASSESS_BUDGET", 4000)                 # credits allowed for /assess in total
MAX_SPEND = knob("MAX_SPEND", 25000)                        # hard ceiling on credits we will ever use
CALIBRATION_SAMPLES = knob("CALIBRATION_SAMPLES", 40)       # /assess calls bought to train the model
MARKET_INTERVAL = knob("MARKET_INTERVAL", 300)              # seconds between market top-ups
RATE_PER_SEC = knob("RATE_PER_SEC", 8.5)                    # stay under the 10 req/s team limit
WORKERS = knob("WORKERS", 6)
STATE_PATH = os.environ.get("STATE_PATH", "state.json")
LOG_PATH = os.environ.get("LOG_PATH", "agent.log")
SAMPLE_DIR = os.environ.get("SAMPLE_DIR", "samples")

COST = {"search": 1, "batch": 60, "assess": 25, "offer": 10, "offer_closing": 20, "release": 5, "market": 2}


def log(*msg):
    line = time.strftime("%H:%M:%S ") + " ".join(str(m) for m in msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ client with a team-wide throttle
class ThrottledArena(Arena):
    """Arena client that spaces requests out so bursts from many threads never hit 429 in the first place.
    Also saves the first response of every endpoint to disk, so we can see the real data shapes."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._gate = threading.Lock()
        self._next_slot = 0.0
        self._sampled = set()

    def _call(self, path, method="GET", body=None, tries=6):
        with self._gate:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + 1.0 / RATE_PER_SEC
        if wait > 0:
            time.sleep(wait)
        result = super()._call(path, method, body, tries)
        self._save_sample(path, result)
        return result

    def _save_sample(self, path, result):
        endpoint = path.strip("/").split("/")[0].split("?")[0] or "root"
        if endpoint in self._sampled:
            return
        self._sampled.add(endpoint)
        try:
            os.makedirs(SAMPLE_DIR, exist_ok=True)
            with open(os.path.join(SAMPLE_DIR, endpoint + ".json"), "w") as f:
                json.dump(result, f, indent=1, default=str)
        except OSError:
            pass


# ------------------------------------------------------------------ persistent state
class State:
    """Everything we have paid for. Saved atomically after each step; reloaded on restart."""

    FIELDS = {"summaries": dict, "pages_done": dict, "profiles": dict, "assessments": dict,
              "signed": dict, "note_scores": dict, "dead": list, "taken_identities": list, "spend": dict, "seen_open": bool, "seen_closing": bool}

    def __init__(self, path):
        self.path, self.lock = path, threading.RLock()
        data = {}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                log(f"resumed state: {len(data.get('profiles', {}))} profiles, {len(data.get('signed', {}))} signed")
            except (OSError, ValueError) as e:
                log(f"state file unreadable ({e}); starting fresh")
        for name, kind in self.FIELDS.items():
            setattr(self, name, data.get(name, kind()))
        self.dead = set(self.dead)                          # candidate ids we will never offer
        self.taken_identities = set(self.taken_identities)  # identity keys of people we hold

    def save(self):
        with self.lock:
            data = {n: getattr(self, n) for n in self.FIELDS}
            data["dead"], data["taken_identities"] = sorted(self.dead), sorted(self.taken_identities)
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w") as f:
                    json.dump(data, f, default=str)
                os.replace(tmp, self.path)
            except OSError as e:
                log(f"could not save state: {e}")

    def add_spend(self, kind, credits):
        with self.lock:
            self.spend[kind] = self.spend.get(kind, 0) + credits


def as_list(response, *keys):
    """Endpoints may wrap lists ({'results': [...]}) or return them bare, or keyed by id."""
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        for k in keys:
            v = response.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                return [dict(val, candidate_id=cid) if isinstance(val, dict) else val for cid, val in v.items()]
    return []


# ------------------------------------------------------------------ the agent
class Agent:
    def __init__(self):
        self.arena = ThrottledArena()
        self.state = State(STATE_PATH)
        self.reqs = {}
        self.pool = ThreadPoolExecutor(max_workers=WORKERS)
        self.model = AssessmentModel()
        self.notes = NoteScorer(self.state.note_scores)
        self.relearn()

    # --- budget -------------------------------------------------------
    def credits_used(self):
        remaining = self.arena.credits_remaining
        return 50000 - remaining if remaining is not None else sum(self.state.spend.values())

    def can_spend(self, credits, expected_points):
        """The one spend rule: stay under the ceiling and only buy what is worth its penalty."""
        if self.credits_used() + credits > MAX_SPEND:
            return False
        if self.arena.credits_remaining is not None and self.arena.credits_remaining < credits:
            return False
        return expected_points > credits * PENALTY_FACTOR

    # --- reading the world ----------------------------------------------
    def phase(self):
        return pick(self.arena.ledger(), "phase", default="unknown")

    def load_requisitions(self, verbose=True):
        raw = as_list(self.arena.requisitions(), "requisitions", "results", "items")
        self.reqs = {r.req_id: r for r in (Requisition(x) for x in raw)}
        for r in (self.reqs.values() if verbose else ()):
            log(f"req {r.req_id} {r.role!r} x{r.headcount} pts={r.points:.0f} skills={sorted(r.skills)} "
                f"min_assess={r.min_assessment:.0f} max_notice={r.max_notice:.0f} max_ctc={r.max_ctc:.0f}")

    def signed_in(self, req_id):
        return sum(1 for r in self.state.signed.values() if r == req_id)

    # --- recon: wide cheap sweep ------------------------------------------
    def sweep_role(self, role):
        """Page through /search for one role until the pages run dry or the cap is hit."""
        start = self.state.pages_done.get(role, 0)
        if start < 0:                                     # -1 marks a role we already exhausted
            return
        page = start
        while page < SEARCH_PAGES_PER_ROLE and self.can_spend(COST["search"], 1e9):
            pages = list(range(page, min(page + WORKERS, SEARCH_PAGES_PER_ROLE)))
            results = list(self.pool.map(lambda p: self.arena.search(role=role, page=p, size=100), pages))
            self.state.add_spend("search", len(pages))
            exhausted = False
            for res in results:
                rows = as_list(res, "results", "candidates", "items")
                for row in rows:
                    cid = candidate_id(row)
                    if cid and cid != "None":
                        self.state.summaries[cid] = row
                if len(rows) < 100:
                    exhausted = True
            page = pages[-1] + 1
            self.state.pages_done[role] = -1 if exhausted else page
            if exhausted:
                break
        log(f"sweep {role!r}: {self.state.pages_done[role]} pages, {len(self.state.summaries)} summaries total")
        self.state.save()

    # --- recon: buy the promising profiles --------------------------------
    def ranked_summaries(self, req):
        scored = [(summary_score(s, req), cid) for cid, s in self.state.summaries.items()]
        return [cid for sc, cid in sorted(scored, reverse=True) if sc > 0]

    def buy_profiles(self, ids):
        """Batch-buy full profiles, 50 at a time. Skips ids we already hold."""
        ids = [i for i in dict.fromkeys(ids) if i not in self.state.profiles and i not in self.state.dead]
        chunks = [ids[i:i + 50] for i in range(0, len(ids), 50)]
        affordable = [c for c in chunks if self.can_spend(COST["batch"], 1e9)]

        def fetch(chunk):
            try:
                return as_list(self.arena.batch(chunk), "profiles", "candidates", "results", "items")
            except WrongPhase:
                raise
            except Exception as e:                      # one bad batch must not stop the rest
                log(f"batch failed: {e}")
                return []
        for rows in self.pool.map(fetch, affordable):
            self.state.add_spend("batch", COST["batch"])
            for p in rows:
                if isinstance(p, dict):
                    self.state.profiles[candidate_id(p)] = p
        if affordable:
            log(f"bought {sum(len(c) for c in affordable)} profiles in {len(affordable)} batches")
        self.state.save()

    def deepen(self, req, depth):
        """Make sure the top depth x headcount summaries for this req have full profiles."""
        want = self.ranked_summaries(req)[: depth * req.headcount]
        self.buy_profiles(want)

    # --- judging ----------------------------------------------------------
    def relearn(self):
        """Refit the assessment model from every /assess we have paid for."""
        pairs, outcomes = [], []
        for cid, result in self.state.assessments.items():
            profile = self.state.profiles.get(cid, {})
            verified, bad = verdict_from_assess(result)
            pairs.append((self_assessment(profile), verified))
            outcomes.append((str(pick(profile, "note", "notes", default="")).strip(), bad))
        self.model.fit(pairs, outcomes)

    def judge(self, cid, req):
        """(eligible, value, flags) for one candidate against one requisition, using everything we know."""
        profile = self.state.profiles.get(cid, {})
        note = pick(profile, "note", "notes", default="")
        result = self.state.assessments.get(cid)
        if result:
            verified, bad = verdict_from_assess(result)
            if bad:
                self.state.dead.add(cid)
                return False, 0.0, ["failed_verification"]
            ok, quality, _ = evaluate(profile, req, verified, self.notes.score(note))
            return ok, req.points * quality, []
        estimate = self.model.estimate(self_assessment(profile))
        ok, quality, _ = evaluate(profile, req, estimate, self.notes.score(note))
        flags = risk_flags(profile, req)
        if note and self.model.note_bad_rate(str(note).strip()) > 0.4:
            flags.append("note_linked_to_fakes")
        if estimate is not None and estimate < req.min_assessment + self.model.std:
            flags.append("near_bar")
        return ok, req.points * quality, flags

    def candidates_for(self, req):
        """Eligible, unsigned, not-dead candidates for req, best first: [(value, cid, flags)]."""
        out = []
        for cid in self.state.profiles:
            if cid in self.state.dead or cid in self.state.signed:
                continue
            if identity_keys(self.state.profiles[cid]) & self.state.taken_identities:
                continue
            ok, value, flags = self.judge(cid, req)
            if ok:
                out.append((value, cid, flags))
        return sorted(out, reverse=True)

    def assess(self, cids):
        def check(cid):
            try:
                return cid, self.arena.assess(cid)
            except WrongPhase:
                raise
            except Exception as e:
                log(f"assess {cid} failed: {e}")
                return cid, None
        for cid, result in self.pool.map(check, cids):
            if result is not None:
                self.state.add_spend("assess", COST["assess"])
                self.state.assessments[cid] = result
        self.relearn()
        self.state.save()

    def assess_top_picks(self):
        """Pay 25 for verification where a planned signing hangs on a doubt, best picks first.
        Signings depend on it twice: fakes score zero, and the pairs train the assessment model."""
        flagged = {}                                    # cid -> value of the pick that hangs on it
        for req in self.reqs.values():
            for value, cid, flags in self.candidates_for(req)[: int(req.headcount * 1.5)]:
                if flags and cid not in self.state.assessments:
                    flagged[cid] = max(value, flagged.get(cid, 0))
        allowance = (ASSESS_BUDGET - self.state.spend.get("assess", 0)) // COST["assess"]
        todo = sorted(flagged, key=flagged.get, reverse=True)[: max(0, allowance)]
        # Worth it if it could plausibly protect a slot worth ~30% of the pick's value.
        todo = [c for c in todo if self.can_spend(COST["assess"], 0.3 * flagged[c])]
        if todo:
            self.assess(todo)
            log(f"assessed {len(todo)} flagged top picks; model: {self.model.describe()}")

    def calibrate(self):
        """Buy a small training set for the assessment model: strong candidates across all roles."""
        need = max(0, CALIBRATION_SAMPLES - len(self.state.assessments))
        if not need:
            return
        picks = []
        for req in self.reqs.values():
            picks += [cid for _, cid, _ in self.candidates_for(req)[:need // len(self.reqs) + 1]]
        picks = [c for c in dict.fromkeys(picks) if c not in self.state.assessments][:need]
        if picks and self.can_spend(COST["assess"] * len(picks), 1e9):
            self.assess(picks)
            log(f"calibration: assessed {len(picks)}; model: {self.model.describe()}")

    # --- recon driver ----------------------------------------------------
    def recon(self):
        self.load_requisitions()
        for role in sorted({r.role for r in self.reqs.values() if r.role}):
            self.sweep_role(role)
        for req in self.reqs.values():
            self.deepen(req, PROFILE_DEPTH)
        self.notes.learn(self.arena, (pick(p, "note", "notes") for p in self.state.profiles.values()), log)
        self.calibrate()
        # Requisitions still short on eligible people get a deeper look, a few rounds at most.
        for depth in (PROFILE_DEPTH * 2, PROFILE_CAP):
            short = [r for r in self.reqs.values() if len(self.candidates_for(r)) < BACKUP_FACTOR * r.headcount]
            for req in short:
                self.deepen(req, depth)
        self.notes.learn(self.arena, (pick(p, "note", "notes") for p in self.state.profiles.values()), log)
        self.assess_top_picks()
        self.state.save()
        for req in self.reqs.values():
            log(f"plan {req.req_id}: {len(self.candidates_for(req))} eligible for {req.headcount} slots")

    # --- signing ---------------------------------------------------------
    def offer(self, cid, req_id, cost):
        try:
            result = self.arena.offer(cid, req_id)
        except WrongPhase:
            raise
        except Exception as e:
            log(f"offer {cid}->{req_id} error: {e}")
            return {"accepted": False, "reason": "error"}
        self.state.add_spend("offer", cost)
        return result

    def fill_slots(self, closing=False):
        """Sign the best available people into every open slot, falling back through the backups."""
        cost = COST["offer_closing" if closing else "offer"]
        while True:
            wave = []                                   # (cid, req_id) offers to send together
            claimed_this_wave = set()
            for req in self.reqs.values():
                slots = req.headcount - self.signed_in(req.req_id)
                for value, cid, flags in self.candidates_for(req):
                    if slots <= 0:
                        break
                    if cid in claimed_this_wave or not self.can_spend(cost, value):
                        continue
                    if any(f.startswith("notes_red") or f in ("impossible_experience", "note_linked_to_fakes")
                           for f in flags):
                        continue                        # unverified and doubtful: not worth a slot
                    wave.append((value, cid, req.req_id))
                    claimed_this_wave.add(cid)
                    slots -= 1
            if not wave:
                return
            wave = [(cid, req_id) for _, cid, req_id in sorted(wave, reverse=True)]   # best names first
            results = list(self.pool.map(lambda o: (o, self.offer(o[0], o[1], cost)), wave))
            progress = False
            for (cid, req_id), res in results:
                reason = pick(res, "reason", "error", default="")
                if pick(res, "accepted") is True:
                    self.state.signed[cid] = req_id
                    self.state.taken_identities |= identity_keys(self.state.profiles.get(cid, {}))
                    log(f"SIGNED {cid} -> {req_id}")
                    progress = True
                elif reason == "requisition_full":
                    self.reqs[req_id].headcount = self.signed_in(req_id)   # stop trying this req
                    progress = True
                else:
                    log(f"offer {cid}->{req_id} rejected: {reason}")
                    self.state.dead.add(cid)            # already_signed / same_person / role_mismatch
                    progress = True
            self.state.save()
            if not progress:
                return

    def top_up(self, closing=False):
        """Market maintenance: refresh the bar, buy a deeper tier where short, fill open slots."""
        try:
            self.load_requisitions(verbose=False)
        except Exception as e:
            log(f"requisitions refresh failed: {e}")
        for req in self.reqs.values():
            open_slots = req.headcount - self.signed_in(req.req_id)
            if open_slots > 0 and len(self.candidates_for(req)) < open_slots * 2 and not closing:
                bought = sum(1 for c in self.ranked_summaries(req) if c in self.state.profiles)
                self.deepen(req, min(PROFILE_CAP, bought // max(1, req.headcount) + PROFILE_DEPTH))
        self.fill_slots(closing)
        led = self.arena.ledger()
        log(f"ledger: credits_used={pick(led, 'credits_used')} points={pick(led, 'points')} "
            f"score={pick(led, 'score')} signed={len(self.state.signed)}")

    # --- main loop -------------------------------------------------------
    def wait_for_change(self, phase, poll=1.0):
        while True:
            now = self.phase()
            if now != phase:
                return now
            time.sleep(poll)

    def run(self):
        recon_done = False
        while True:
            phase = self.phase()
            if phase == "closed":
                # Only 'closed' after the closing hour is the real end. A 'closed' seen earlier is an
                # organiser pause: keep holding and waiting, never exit mid-arena.
                if self.state.seen_closing:
                    log("arena closed - final ledger", self.arena.ledger())
                    return
                time.sleep(5)
                continue
            if not self.state.seen_open:
                self.state.seen_open = True
                self.state.save()
            if phase == "closing" and not self.state.seen_closing:
                self.state.seen_closing = True
                self.state.save()

            if phase == "recon":
                if not recon_done:
                    self.recon()
                    recon_done = True
                log("recon complete; waiting for the market to open")
                self.wait_for_change("recon")
            elif phase == "market":
                if not recon_done and not self.state.profiles:
                    self.recon()                        # started late: do the essentials first
                recon_done = True
                self.top_up()
                deadline = time.time() + MARKET_INTERVAL
                while time.time() < deadline and self.phase() == "market":
                    time.sleep(3)
            elif phase == "closing":
                self.top_up(closing=True)
                deadline = time.time() + MARKET_INTERVAL
                while time.time() < deadline and self.phase() == "closing":
                    time.sleep(5)
            else:
                log(f"unknown phase {phase!r}; waiting")
                time.sleep(5)


def main():
    log(f"agent start: penalty={PENALTY_FACTOR} max_spend={MAX_SPEND} pages/role={SEARCH_PAGES_PER_ROLE}")
    agent = Agent()
    while True:
        try:
            agent.run()
            return
        except Exhausted:
            log("out of credits: only free calls from here; holding until close")
            while True:
                try:
                    if agent.phase() == "closed":
                        return
                except Exception:
                    pass
                time.sleep(30)
        except WrongPhase as e:
            log(f"phase moved under us ({e}); re-reading phase")
            time.sleep(1)
        except KeyboardInterrupt:
            agent.state.save()
            raise
        except Exception:
            # Never let an unexpected error cost one of our two restarts: log, save, carry on.
            log("unexpected error:\n" + traceback.format_exc())
            agent.state.save()
            time.sleep(5)


if __name__ == "__main__":
    main()
