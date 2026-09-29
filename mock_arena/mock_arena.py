"""
Mock Battle Arena: a local copy of the arena API for testing agents offline.

    python mock_arena.py --port 8765 --recon 90 --market 40 --closing 15

It mirrors the real arena's behaviour as closely as we observed it on the day:
    - the same 10 endpoints, costs, phases and response shapes
    - the 10 real requisitions (skills, headcount, bar)
    - a generated candidate pool with the real messiness: mixed skill separators and spellings,
      experience in months/years, CTC in rupees or LPA, notice as text, assessments as
      "72", "72/100", "0.72", "7.2/10" or empty
    - fabricated profiles (self-score 85-99, verified 25-55, identity not verified)
    - duplicate people under different ids
    - 10 req/s rate limit per key (burst 30) -> 429 rate_limited, and 429 credits_exhausted
    - rival teams that start signing good-looking candidates the moment the market opens
    - an optional organiser pause (phase briefly reports "closed" mid-market), like the 11:49 glitch

Test-only endpoint:  GET /_debug/holdings  -> the truth about every candidate a key holds.
Standard library only.
"""
import argparse, json, math, random, re, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

# ---------------------------------------------------------------- the 10 requisitions seen on the day
REQUISITIONS = [
    ("REQ-01", "Backend Engineer", ["python", "java", "sql", "rest apis", "microservices", "postgresql"], 9, 75, 26),
    ("REQ-02", "Frontend Engineer", ["javascript", "typescript", "react", "css", "html", "graphql"], 16, 75, 26),
    ("REQ-03", "Full Stack Engineer", ["javascript", "node.js", "react", "sql", "rest apis", "docker"], 11, 65, 35),
    ("REQ-04", "Data Scientist", ["python", "statistics", "scikit-learn", "sql", "pandas", "a/b testing"], 14, 65, 26),
    ("REQ-05", "ML Engineer", ["python", "pytorch", "mlops", "docker", "kubernetes", "scikit-learn"], 10, 70, 26),
    ("REQ-06", "Data Engineer", ["python", "sql", "spark", "airflow", "kafka", "aws"], 10, 75, 26),
    ("REQ-07", "DevOps / SRE", ["linux", "kubernetes", "terraform", "aws", "ci/cd", "docker"], 11, 65, 35),
    ("REQ-08", "Mobile Engineer", ["kotlin", "swift", "android", "flutter", "rest apis", "git"], 11, 65, 22),
    ("REQ-09", "QA Automation Engineer", ["selenium", "python", "java", "cypress", "ci/cd", "sql"], 14, 70, 26),
    ("REQ-10", "Product Analyst", ["sql", "excel", "tableau", "statistics", "a/b testing", "python"], 10, 75, 30),
]
MAX_NOTICE = 60
POINTS_NOTE = ("points scale with skill match, assessment margin over the bar, recruiter notes, "
               "notice period and salary headroom; below the bar earns nothing")

# How the same skill is spelled in messy profiles
SPELLINGS = {
    "python": ["Python", "python", "Python3", "py"], "java": ["Java", "java"], "sql": ["SQL", "Sql", "sql"],
    "rest apis": ["REST", "Rest Apis", "RESTful APIs", "REST API"], "microservices": ["Microservices", "microservice"],
    "postgresql": ["Postgres", "PostgreSQL", "Postgresql", "psql"], "javascript": ["JS", "JavaScript", "Javascript"],
    "typescript": ["TS", "TypeScript"], "react": ["React", "ReactJS", "React.js"], "css": ["CSS", "CSS3"],
    "html": ["HTML", "HTML5"], "graphql": ["GraphQL", "gql"], "node.js": ["Node", "NodeJS", "Node.js"],
    "docker": ["Docker", "docker"], "statistics": ["Statistics", "Stats"], "scikit-learn": ["sklearn", "Scikit-learn", "scikit learn"],
    "pandas": ["Pandas", "pandas"], "a/b testing": ["A/B Testing", "AB testing", "A/B tests"], "pytorch": ["PyTorch", "torch"],
    "mlops": ["MLOps", "ML Ops"], "kubernetes": ["k8s", "Kubernetes", "K8s"], "spark": ["Spark", "PySpark", "Apache Spark"],
    "airflow": ["Airflow", "Apache Airflow"], "kafka": ["Kafka", "Apache Kafka"], "aws": ["AWS", "Amazon Web Services"],
    "linux": ["Linux"], "terraform": ["Terraform"], "ci/cd": ["CI/CD", "CICD", "ci cd"], "kotlin": ["Kotlin"],
    "swift": ["Swift"], "android": ["Android"], "flutter": ["Flutter"], "git": ["Git", "git"],
    "selenium": ["Selenium", "Selenium WebDriver"], "cypress": ["Cypress"], "excel": ["Excel", "MS Excel"], "tableau": ["Tableau"],
}
NOISE_SKILLS = ["Redis", "Django", "Flask", "Numpy", "Computer Vision", "Angular", "Mongodb", "Go", "Rust", "Figma"]
NOTES = [("", 0), ("asked about relocation", 0), ("prefers hybrid work", 0), ("has a planned vacation", 0),
         ("shipped a feature used by a million users", 1), ("led a migration off a legacy service", 1),
         ("owned production incidents during on-call", 1), ("strong communicator, great references", 1),
         ("exploring other offers", -1), ("reference check was lukewarm", -1), ("missed two sprint commitments", -1)]
CITIES = ["Delhi", "Mumbai", "Bengaluru", "Pune", "Hyderabad", "Chennai", "Kolkata", "Gurugram", "Noida", "Jaipur"]
FIRST = ["Aarav", "Priya", "Rohan", "Ananya", "Kabir", "Meera", "Arjun", "Isha", "Vivaan", "Neha", "Aditya", "Sara",
         "Karthik", "Diya", "Ishaan", "Riya", "Varun", "Pooja", "Nikhil", "Tanvi"]
LAST = ["Sharma", "Mehta", "Singh", "Khan", "Das", "Nair", "Iyer", "Gupta", "Reddy", "Pillai", "Banerjee", "Ghosh",
        "Rao", "Joshi", "Verma", "Chopra"]


# ---------------------------------------------------------------- candidate generation
def fmt_experience(rng, years):
    return rng.choice([f"{years:.1f} yrs", f"{years:.1f} years", f"{round(years * 12)} months", f"{int(years)}+ years", f"{years:.1f}"])


def fmt_ctc(rng, lpa):
    return rng.choice([str(int(lpa * 100000)), f"{lpa:.1f} LPA"])


def fmt_assessment(rng, score):
    if rng.random() < 0.05:
        return ""
    return rng.choice([str(score), f"{score}/100", f"{score / 100:.2f}", f"{score / 10:.1f}/10"])


def make_pool(size, seed):
    rng = random.Random(seed)
    pool, persons = {}, 0
    for i in range(size):
        rid, role, skills, *_ = rng.choice(REQUISITIONS)
        n_core = rng.choices([1, 2, 3, 4, 5, 6], weights=[8, 12, 18, 22, 22, 18])[0]
        chosen = [rng.choice(SPELLINGS[s]) for s in rng.sample(skills, n_core)]
        chosen += rng.sample(NOISE_SKILLS, rng.randint(0, 3))
        rng.shuffle(chosen)
        sep = rng.choice([", ", "; ", " | "])
        fake = rng.random() < 0.06
        self_score = rng.randint(85, 99) if fake else min(100, max(30, int(rng.gauss(72, 14))))
        verified = rng.randint(25, 55) if fake else max(0, self_score - rng.randint(0, 10))
        years = round(rng.uniform(0.5, 14), 1)
        lpa = round(rng.uniform(4, 40), 1)
        notice = rng.choice([0, 15, 30, 30, 45, 60, 60, 90, 90])
        note, sentiment = rng.choice(NOTES)
        cid = f"MK-{i:07d}"
        pool[cid] = {
            "candidate_id": cid, "person": persons, "name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
            "role": role, "city": rng.choice(CITIES), "experience": fmt_experience(rng, years),
            "skills": "" if rng.random() < 0.02 else sep.join(chosen),
            "current_ctc": fmt_ctc(rng, lpa * rng.uniform(0.7, 0.95)), "expected_ctc": fmt_ctc(rng, lpa),
            "notice_period": "Immediate" if notice == 0 else rng.choice([f"{notice} days", f"{notice // 30} months" if notice % 30 == 0 else f"{notice} days"]),
            "assessment": fmt_assessment(rng, self_score), "note": note,
            # hidden truth, never sent to agents
            "_fake": fake, "_verified": verified, "_self": self_score, "_notice": notice, "_lpa": lpa,
            "_core": {s for s in skills if any(v in chosen for v in SPELLINGS[s])}, "_sentiment": sentiment,
        }
        persons += 1
        if rng.random() < 0.02:                                  # same person again under a second id
            dup = dict(pool[cid], candidate_id=f"MK-{size + i:07d}")
            pool[dup["candidate_id"]] = dup
    return pool


# ---------------------------------------------------------------- arena state
class Arena:
    def __init__(self, args):
        self.args, self.lock = args, threading.RLock()
        self.pool = make_pool(args.pool, args.seed)
        self.by_role = {}
        for c in self.pool.values():
            self.by_role.setdefault(c["role"], []).append(c["candidate_id"])
        self.t0 = time.time()
        self.holder = {}                      # person id -> (key, candidate id, req id)
        self.teams = {}                       # key -> team dict
        self.rng = random.Random(args.seed + 1)
        threading.Thread(target=self.rivals, daemon=True).start()

    # -- phases -----------------------------------------------------
    def phase(self):
        a = self.args
        t = elapsed = time.time() - self.t0
        marks = [("closed", a.start), ("recon", a.recon), ("market", a.market), ("closing", a.closing)]
        for name, dur in marks:
            if t < dur:
                if name == "market" and a.pause_at and a.pause_at <= elapsed < a.pause_at + a.pause_len:
                    return "closed"               # organiser pause, like the 11:49 glitch
                return name
            t -= dur
        return "closed"

    def team(self, key):
        if key not in self.teams:
            self.teams[key] = {"credits": 50000, "calls": 0, "tokens": time.time(), "bucket": 30.0,
                               "held": {}, "bought": {}, "log": []}
        return self.teams[key]

    # -- rules -------------------------------------------------------
    def req(self, rid):
        for r in REQUISITIONS:
            if r[0] == rid:
                return r
        return None

    def points(self, c, rid):
        """Hidden scoring, following the organisers' points note. Fakes and below-bar people earn 0."""
        _, role, skills, _, bar, max_lpa = self.req(rid)
        if c["_fake"] or c["role"] != role or c["_verified"] < bar or c["_notice"] > MAX_NOTICE or c["_lpa"] > max_lpa:
            return 0.0
        skill = len(c["_core"]) / len(skills)
        margin = min(1.0, (c["_verified"] - bar) / 25)
        headroom = min(1.0, 2 * (max_lpa - c["_lpa"]) / max_lpa)
        notice = 1 - c["_notice"] / MAX_NOTICE
        note = (c["_sentiment"] + 1) / 2
        return round(10 * skill + 6 * margin + 3 * headroom + 3 * notice + 3 * note, 2)

    def ledger(self, key):
        t = self.team(key)
        pts = sum(self.points(self.pool[cid], rid) for cid, rid in t["held"].items())
        used = 50000 - t["credits"]
        return {"team": key, "credits_used": used, "credits_remaining": t["credits"], "calls": t["calls"],
                "signed": len(t["held"]), "points": round(pts), "score": round(pts - used * self.args.penalty, 1),
                "points_as_of": time.time(), "phase": self.phase()}

    def rate_ok(self, t):
        now = time.time()
        t["bucket"] = min(30.0, t["bucket"] + (now - t["tokens"]) * 10)
        t["tokens"] = now
        if t["bucket"] < 1:
            return False
        t["bucket"] -= 1
        return True

    # -- rivals ------------------------------------------------------
    def rivals(self):
        """Naive rival teams: once the market opens they sign high self-score candidates (fakes included)."""
        while True:
            time.sleep(0.1)
            if self.phase() != "market":
                continue
            elapsed = time.time() - self.t0 - self.args.start - self.args.recon
            rate = self.args.rivals if elapsed < 10 else self.args.rivals / 8
            with self.lock:
                for _ in range(max(1, int(rate / 10))):
                    rid, role, *_ = self.rng.choice(REQUISITIONS)
                    ids = self.by_role[role]
                    cid = max(self.rng.sample(ids, 40), key=lambda i: self.pool[i]["_self"])
                    person = self.pool[cid]["person"]
                    if person not in self.holder:
                        self.holder[person] = ("rival", cid, rid)


# ---------------------------------------------------------------- HTTP layer
COST = {"search": 1, "candidate": 2, "batch": 60, "assess": 25, "offer": 10, "release": 5, "market": 2}


def summary(c):
    return {k: c[k] for k in ("candidate_id", "name", "role", "city", "experience", "skills")}


def profile(c, arena):
    p = {k: v for k, v in c.items() if not k.startswith("_") and k != "person"}
    p["claimed"] = c["person"] in arena.holder
    return p


def make_handler(arena):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, obj, key=None):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            if key:
                self.send_header("X-Credits-Remaining", str(arena.team(key)["credits"]))
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def body(self):
            n = int(self.headers.get("content-length") or 0)
            if n > 65536:
                return None
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return None

        def handle_any(self, method):
            url = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            path = url.path
            if path == "/health":
                return self.send(200, {"ok": True, "phase": arena.phase(), "teams": len(arena.teams)})
            key = self.headers.get("X-Arena-Key", "")
            if not key:
                return self.send(401, {"detail": "unknown or missing X-Arena-Key"})
            with arena.lock:
                t = arena.team(key)
                if not arena.rate_ok(t):
                    return self.send(429, {"error": "rate_limited"}, key)
                t["calls"] += 1
                if path == "/_debug/holdings":
                    return self.send(200, [dict(cid=cid, req=rid, fake=arena.pool[cid]["_fake"], verified=arena.pool[cid]["_verified"],
                                                points=arena.points(arena.pool[cid], rid)) for cid, rid in t["held"].items()]
                                     + [{"duplicate_profile_buys": sum(v - 1 for v in t["bought"].values() if v > 1)}])
                if path == "/ledger":
                    return self.send(200, arena.ledger(key), key)
                if path == "/requisitions":
                    held = {}
                    for rid in t["held"].values():
                        held[rid] = held.get(rid, 0) + 1
                    return self.send(200, [{"req_id": r[0], "role": r[1], "skills": r[2], "headcount": r[3], "min_assessment": r[4],
                                            "max_notice_days": MAX_NOTICE, "max_expected_ctc_lpa": r[5], "points_note": POINTS_NOTE,
                                            "filled": held.get(r[0], 0), "remaining": r[3] - held.get(r[0], 0)} for r in REQUISITIONS], key)
                phase = arena.phase()
                if phase == "closed":
                    return self.send(409, {"error": "wrong_phase", "phase": phase}, key)
                data = self.body() if method in ("POST",) else {}
                if data is None:
                    return self.send(422, {"error": "bad_request"}, key)
                return self.route(method, path, q, data, key, t, phase)

        def charge(self, key, t, cost):
            if t["credits"] < cost:
                self.send(429, {"error": "credits_exhausted"}, key)
                return False
            t["credits"] -= cost
            return True

        def route(self, method, path, q, data, key, t, phase):
            if method == "GET" and path == "/search":
                if not self.charge(key, t, COST["search"]):
                    return
                ids = arena.by_role.get(q.get("role"), []) if q.get("role") else list(arena.pool)
                if q.get("city"):
                    ids = [i for i in ids if arena.pool[i]["city"] == q["city"]]
                if q.get("q"):
                    ids = [i for i in ids if q["q"].lower() in (arena.pool[i]["name"] + arena.pool[i]["skills"]).lower()]
                page, size = int(q.get("page", 0)), min(100, int(q.get("size", 100)))
                return self.send(200, {"total": len(ids), "page": page, "size": size,
                                       "results": [summary(arena.pool[i]) for i in ids[page * size:(page + 1) * size]]}, key)
            m = re.fullmatch(r"/candidate/(.+)", path)
            if method == "GET" and m:
                c = arena.pool.get(m.group(1))
                if not c:
                    return self.send(404, {"error": "unknown_candidate"}, key)
                if not self.charge(key, t, COST["candidate"]):
                    return
                t["bought"][c["candidate_id"]] = t["bought"].get(c["candidate_id"], 0) + 1
                return self.send(200, profile(c, arena), key)
            if method == "POST" and path == "/candidates/batch":
                ids = data.get("ids") or []
                if not 1 <= len(ids) <= 50:
                    return self.send(422, {"error": "bad_request"}, key)
                if not self.charge(key, t, COST["batch"]):
                    return
                for i in ids:
                    t["bought"][i] = t["bought"].get(i, 0) + 1
                return self.send(200, {"candidates": [profile(arena.pool[i], arena) for i in ids if i in arena.pool]}, key)
            m = re.fullmatch(r"/assess/(.+)", path)
            if method == "GET" and m:
                c = arena.pool.get(m.group(1))
                if not c:
                    return self.send(404, {"error": "unknown_candidate"}, key)
                if not self.charge(key, t, COST["assess"]):
                    return
                return self.send(200, {"candidate_id": c["candidate_id"], "verified_assessment": c["_verified"],
                                       "reference_check": "could not verify employment" if c["_fake"] else "clean",
                                       "identity_verified": not c["_fake"]}, key)
            if method == "GET" and path == "/market":
                if not self.charge(key, t, COST["market"]):
                    return
                counts = {}
                for _, _, rid in arena.holder.values():
                    counts[rid] = counts.get(rid, 0) + 1
                return self.send(200, {"signings": counts, "your_rank": None}, key)
            if method == "POST" and path == "/offer":
                return self.offer(data, key, t, phase)
            m = re.fullmatch(r"/offer/(.+)", path)
            if method == "DELETE" and m:
                cid = m.group(1)
                if cid not in t["held"]:
                    return self.send(404, {"error": "not_held"}, key)
                if not self.charge(key, t, COST["release"]):
                    return
                del t["held"][cid]
                arena.holder.pop(arena.pool[cid]["person"], None)
                return self.send(200, {"released": True}, key)
            if method == "POST" and path == "/reason":
                return self.reason(data, key, t)
            return self.send(404, {"detail": "Not Found"}, key)

        def offer(self, data, key, t, phase):
            if phase == "recon":
                return self.send(409, {"error": "wrong_phase", "phase": phase}, key)
            cid, rid = data.get("candidate_id"), data.get("req_id")
            c, r = arena.pool.get(cid), arena.req(rid)
            if not c or not r:
                return self.send(404, {"error": "unknown_candidate_or_requisition"}, key)
            if not self.charge(key, t, 20 if phase == "closing" else 10):
                return
            holder = arena.holder.get(c["person"])
            if t["held"].get(cid) == rid:
                return self.send(200, {"accepted": True, "already_yours": True}, key)
            if holder and holder[0] == key:
                return self.send(200, {"accepted": False, "reason": "same_person_already_signed"}, key)
            if holder:
                return self.send(200, {"accepted": False, "reason": "already_signed"}, key)
            if c["role"] != r[1]:
                return self.send(200, {"accepted": False, "reason": "role_mismatch"}, key)
            if sum(1 for x in t["held"].values() if x == rid) >= r[3]:
                return self.send(200, {"accepted": False, "reason": "requisition_full"}, key)
            t["held"][cid] = rid
            arena.holder[c["person"]] = (key, cid, rid)
            return self.send(200, {"accepted": True}, key)

        def reason(self, data, key, t):
            """Stand-in for the hosted LLM: scores recruiter notes by keywords, in the same fenced-JSON shape."""
            prompt = str(data.get("prompt", ""))
            tokens = len(prompt) // 4 + 60
            if tokens > 4000:
                return self.send(422, {"error": "too_many_tokens"}, key)
            if not self.charge(key, t, max(1, math.ceil(tokens / 1000))):
                return
            try:
                notes = json.loads(prompt.split("Notes:", 1)[1].strip())
            except (IndexError, ValueError):
                notes = []
            neg = r"lukewarm|other offers|missed|fail|could not"
            pos = r"shipped|led|owned|strong|great"
            scores = {n: (-2 if re.search(neg, n) else 1 if re.search(pos, n) else 0) for n in notes}
            return self.send(200, {"completion": "```json\n" + json.dumps(scores) + "\n```", "tokens": tokens}, key)

        def do_GET(self):
            self.handle_any("GET")

        def do_POST(self):
            self.handle_any("POST")

        def do_DELETE(self):
            self.handle_any("DELETE")
    return Handler


def main():
    ap = argparse.ArgumentParser(description="Mock Battle Arena")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--pool", type=int, default=20000, help="candidates in the pool")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--start", type=float, default=3, help="seconds before the arena opens")
    ap.add_argument("--recon", type=float, default=90)
    ap.add_argument("--market", type=float, default=40)
    ap.add_argument("--closing", type=float, default=15)
    ap.add_argument("--pause-at", type=float, default=0, help="seconds into the whole run to fake a 'closed' pause (0 = off)")
    ap.add_argument("--pause-len", type=float, default=4)
    ap.add_argument("--rivals", type=float, default=40, help="rival signings per second in the first 10 s of market")
    ap.add_argument("--penalty", type=float, default=0.05)
    args = ap.parse_args()
    arena = Arena(args)
    print(f"mock arena on http://127.0.0.1:{args.port}  pool={len(arena.pool)}  "
          f"phases: closed {args.start}s, recon {args.recon}s, market {args.market}s, closing {args.closing}s", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(arena)).serve_forever()


if __name__ == "__main__":
    main()
