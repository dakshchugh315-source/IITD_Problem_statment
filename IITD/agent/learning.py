"""What the agent learns while it runs. Both models are trained only on data the agent paid for.

AssessmentModel
    Self-reported assessments are inflated. Every /assess we buy gives a (self-reported, verified)
    pair; we fit  verified ~ a * self + b  by least squares and keep the residual spread. Unassessed
    candidates are then judged on a conservative estimate (prediction minus half a std), so the
    agent stops trusting face values without paying 25 credits for every candidate.
    It also learns which recruiter notes go with fabricated / failed-reference profiles.

NoteScorer
    Recruiter notes come from a limited vocabulary. Each distinct note is scored once, -2..+2, by
    the /reason model in batches (a few credits in total), cached, and reused for every profile.
    A keyword fallback covers the time before (or without) the model.
"""
import json, re, statistics

from normalize import pick, parse_score_100


def verdict_from_assess(result):
    """(verified_score or None, is_bad) from a /assess response.
    Bad = identity not verified, or a reference check that is not clean."""
    score = parse_score_100(pick(result, "verified_assessment", "assessment", "score"))
    identity = pick(result, "identity_verified")
    ref = str(pick(result, "reference_check", default="clean")).lower()
    bad = identity is False or any(w in ref for w in ("fail", "neg", "concern", "fabricat", "fake", "flag", "mismatch"))
    if pick(result, "fabricated", "is_fabricated", "fake") is True:
        bad = True
    return score, bad


class AssessmentModel:
    MIN_PAIRS = 12

    def __init__(self):
        self.a, self.b, self.std = 1.0, -4.0, 6.0     # prior until we have data: mild inflation
        self.n = 0
        self.bad_by_note = {}                          # note -> [bad count, total count]

    def fit(self, pairs, note_outcomes):
        """pairs: [(self_reported, verified)]; note_outcomes: [(note, is_bad)]."""
        self.bad_by_note = {}
        for note, bad in note_outcomes:
            c = self.bad_by_note.setdefault(note, [0, 0])
            c[0] += int(bad)
            c[1] += 1
        pairs = [(s, v) for s, v in pairs if s is not None and v is not None]
        self.n = len(pairs)
        if self.n < self.MIN_PAIRS:
            if pairs:                                  # too few for a slope: learn the average gap only
                self.b = statistics.fmean(v - s for s, v in pairs)
            return
        x, y = [float(s) for s, _ in pairs], [float(v) for _, v in pairs]
        mx, my = statistics.fmean(x), statistics.fmean(y)
        var = sum((xi - mx) ** 2 for xi in x)
        self.a = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / var if var else 1.0
        self.b = my - self.a * mx
        self.std = max(2.0, statistics.pstdev(yi - (self.a * xi + self.b) for xi, yi in zip(x, y)))

    def estimate(self, self_reported):
        """Conservative estimate of the verified score."""
        if self_reported is None:
            return None
        return float(self.a * self_reported + self.b - 0.5 * self.std)

    def note_bad_rate(self, note):
        bad, total = self.bad_by_note.get(note, (0, 0))
        return (bad + 0.1) / (total + 1)               # smoothed

    def describe(self):
        return f"verified ~ {self.a:.2f}*self {self.b:+.1f} (std {self.std:.1f}, n={self.n})"


NEGATIVE = r"fail|fake|fabricat|unverif|could not|inconsisten|discrepan|red flag|suspicious|no[- ]show|" \
           r"backed out|declin|not interested|other offers|counter|flight risk|poor|weak|lied|inflat|job hop"
POSITIVE = r"strong|excellent|outstanding|highly recommend|top|impressive|exceptional|great|solid|keen|eager|" \
           r"immediate joiner|verified|references? (are )?(glowing|positive|strong)"


def clip(x, lo=-2.0, hi=2.0):
    return float(max(lo, min(hi, x)))


def keyword_note_score(note):
    text = str(note or "").lower()
    if not text.strip():
        return 0.0
    return clip(len(re.findall(POSITIVE, text)) - 1.5 * len(re.findall(NEGATIVE, text)))


class NoteScorer:
    def __init__(self, cache):
        self.cache = cache                             # dict note -> score, persisted in state

    def score(self, note):
        note = str(note or "").strip()
        if not note:
            return 0.0
        return self.cache.get(note, keyword_note_score(note))

    def learn(self, arena, notes, log, batch=40):
        """Score unseen notes with the /reason model. A handful of calls; failures fall back to keywords."""
        todo = [n for n in dict.fromkeys(str(x).strip() for x in notes) if n and n not in self.cache]
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            prompt = ("You screen candidates for a recruiter. Rate each recruiter note from -2 (serious "
                      "red flag: fabricated, failed references, likely to not join) to +2 (strong hire "
                      "signal). 0 is neutral. Reply ONLY with a JSON object mapping each note exactly "
                      "to its integer score.\nNotes: " + json.dumps(chunk))
            try:
                res = arena.reason(prompt, max_tokens=min(3000, 25 * len(chunk) + 50))
                text = str(pick(res, "completion", "text", "output", default=""))
                parsed = json.loads(text[text.index("{"): text.rindex("}") + 1])
                for note in chunk:
                    if note in parsed:
                        self.cache[note] = clip(float(parsed[note]))
            except Exception as e:                     # model down, bad JSON, 503: keywords it is
                log(f"note scoring fell back to keywords: {e}")
                return
        if todo:
            log(f"scored {len(todo)} distinct recruiter notes with the model")
