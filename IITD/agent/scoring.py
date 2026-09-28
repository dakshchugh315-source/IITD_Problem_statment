"""Judging fit, risk and identity. Pure functions: no API calls, no credits.

Three questions per (candidate, requisition):
    summary_score  - is this summary worth paying 1.2 credits to read in full?
    evaluate       - does the full profile clear the bar, and how well?
    risk_flags     - does anything suggest the profile is fabricated or inflated?
"""
import re
from normalize import (pick, skill_set, parse_notice_days, parse_money_rupees, parse_score_100,
                       parse_years, norm_name, norm_role)


class Requisition:
    """A normalised view of one /requisitions entry, whatever the exact key spellings are."""

    def __init__(self, raw):
        self.raw = raw
        bar = pick(raw, "bar", default={}) or {}
        get = lambda *names: pick(bar, *names) if pick(bar, *names) is not None else pick(raw, *names)
        self.req_id = str(pick(raw, "req_id", "id", "requisition_id"))
        self.role = pick(raw, "role", "title", default="")
        self.role_key = norm_role(self.role)
        self.skills = skill_set(get("skills", "skills_wanted", "required_skills", "must_have"))
        self.nice_skills = skill_set(get("nice_to_have", "preferred_skills", "optional_skills"))
        self.headcount = int(pick(raw, "headcount", default=1) or 1)
        self.min_assessment = parse_score_100(get("min_assessment", "min_score")) or 0.0
        notice = parse_notice_days(get("max_notice_days", "max_notice"))
        self.max_notice = notice if notice is not None else 1e9
        ctc = parse_money_rupees(get("max_expected_ctc_lpa", "max_expected_ctc", "max_ctc", "budget", "max_budget"))
        self.max_ctc = ctc if ctc is not None else 1e18
        self.min_years = parse_years(get("min_experience", "min_years", "min_experience_years")) or 0.0
        self.city = get("city", "location")
        self.points = float(pick(raw, "points", "points_on_offer", "max_points", "value", default=100) or 100)

    def remaining(self, signed_here):
        """Open slots for us. Trust the server's count when it gives one."""
        server = pick(self.raw, "remaining", "remaining_slots", "open_slots")
        if isinstance(server, (int, float)):
            return int(server)
        return max(0, self.headcount - signed_here)


def candidate_id(record):
    return str(pick(record, "candidate_id", "id", "cid"))


def skill_overlap(cand_skills, req):
    """Share of the requisition's wanted skills the candidate has, plus a small nice-to-have bonus."""
    if not req.skills:
        return 0.5
    core = len(cand_skills & req.skills) / len(req.skills)
    bonus = 0.1 * len(cand_skills & req.nice_skills) / max(1, len(req.nice_skills))
    return min(1.0, core + bonus)


def summary_score(summary, req):
    """Cheap ranking from a search summary. Higher is more worth buying the full profile."""
    if req.role_key and norm_role(pick(summary, "role", "title")) != req.role_key:
        return -1.0
    score = skill_overlap(skill_set(pick(summary, "skills")), req)
    years = parse_years(pick(summary, "experience", "years_experience", "experience_years"))
    if years is not None and years < req.min_years:
        score -= 0.5
    if req.city and pick(summary, "city", "location"):
        if str(req.city).lower() in str(pick(summary, "city", "location")).lower():
            score += 0.05
    return score


# Phrases in recruiter notes. Counted, not interpreted: the notes are free text.
RED_FLAGS = [
    r"could not (be )?verif", r"unverif", r"not verified", r"fake", r"fabricat", r"inflat", r"exaggerat",
    r"inconsisten", r"discrepan", r"red flag", r"suspicious", r"plagiar", r"cheat", r"proxy",
    r"reference(s)? (check )?(failed|negative|poor|bad)", r"no[- ]show", r"ghost", r"backed out",
    r"declin(ed|es) offer", r"not (actually )?interested", r"does not exist", r"mismatch", r"dubious",
    r"copied", r"duplicate", r"lied", r"misrepresent", r"blacklist", r"terminated", r"fired",
]
GREEN_FLAGS = [
    r"strong", r"excellent", r"outstanding", r"highly recommend", r"verified", r"top (performer|\d+%)",
    r"impressive", r"exceptional", r"solid", r"great (fit|communicat)", r"keen", r"eager",
]


def note_signal(profile):
    notes = " ".join(str(v) for k, v in profile.items()
                     if any(w in str(k).lower() for w in ("note", "comment", "remark", "feedback")))
    text = notes.lower()
    red = sum(1 for p in RED_FLAGS if re.search(p, text))
    green = sum(1 for p in GREEN_FLAGS if re.search(p, text)
                and not re.search(r"(not|never|n't)\s+\w*\s*" + p, text))
    return red, green


def risk_flags(profile, req):
    """Reasons to doubt a profile. Each flag is a short label; an empty list means no doubt."""
    flags = []
    red, _ = note_signal(profile)
    if red:
        flags.append(f"notes_red_{red}")
    assessment = parse_score_100(pick(profile, "assessment", "assessment_score", "score"))
    if assessment is None:
        flags.append("no_assessment")
    elif assessment >= 97:
        flags.append("suspiciously_perfect")
    elif assessment < req.min_assessment + 5:
        flags.append("near_bar")
    years = parse_years(pick(profile, "experience", "years_experience", "experience_years"))
    age = parse_years(pick(profile, "age"))
    if years is not None and (years > 45 or (age is not None and years > age - 16)):
        flags.append("impossible_experience")
    skills = skill_set(pick(profile, "skills"))
    if years is not None and years < 2 and len(skills) > 15:
        flags.append("skill_stuffing")
    return flags


def self_assessment(profile):
    return parse_score_100(pick(profile, "assessment", "assessment_score", "score"))


def evaluate(profile, req, assessment=None, note_score=0.0):
    """Return (eligible, quality 0..1, reasons).

    `assessment` is our best estimate of the VERIFIED score: the paid /assess value when we have
    it, otherwise the learned correction of the self-reported one. `note_score` is -2..+2.
    Quality mirrors what the organisers say points scale with: skill match, assessment margin over
    the bar, recruiter notes, notice period and salary headroom.
    """
    reasons = []
    if req.role_key and norm_role(pick(profile, "role", "title")) not in ("", req.role_key):
        return False, 0.0, ["role_mismatch"]
    if pick(profile, "claimed", "signed", "taken") is True:
        return False, 0.0, ["claimed"]
    if assessment is None:
        assessment = self_assessment(profile)
    notice = parse_notice_days(pick(profile, "notice_period", "notice", "notice_days"))
    ctc = parse_money_rupees(pick(profile, "expected_ctc", "ctc_expected", "expected_salary"))
    if assessment is None:
        return False, 0.0, ["no_assessment"]
    if assessment < req.min_assessment:
        return False, 0.0, [f"assessment {assessment:.0f}<{req.min_assessment:.0f}"]
    if notice is not None and notice > req.max_notice:
        return False, 0.0, [f"notice {notice:.0f}>{req.max_notice:.0f}"]
    if ctc is not None and ctc > req.max_ctc:
        return False, 0.0, ["ctc_over_budget"]

    overlap = skill_overlap(skill_set(pick(profile, "skills")), req)
    margin = min(1.0, (assessment - req.min_assessment) / 25)
    notice_fit = 0.5 if notice is None else max(0.0, 1 - notice / max(req.max_notice, 1))
    headroom = 0.3 if ctc is None or req.max_ctc > 1e15 else min(1.0, 2 * (req.max_ctc - ctc) / req.max_ctc)
    notes = (max(-2.0, min(2.0, note_score)) + 2) / 4
    quality = 0.35 * overlap + 0.25 * margin + 0.15 * notes + 0.10 * notice_fit + 0.15 * headroom
    if note_score <= -2:
        reasons.append("bad_note")
    return overlap >= 0.3 and note_score > -2, max(0.0, min(1.0, quality)), reasons


def identity_keys(record):
    """Keys that identify the same person across ids. Any shared key means 'same person'."""
    keys = set()
    email = pick(record, "email")
    if email:
        keys.add("e:" + str(email).strip().lower())
    phone = re.sub(r"\D", "", str(pick(record, "phone", "mobile", default="")))[-10:]
    if len(phone) == 10:
        keys.add("p:" + phone)
    name = norm_name(pick(record, "name", "full_name"))
    if name:
        city = str(pick(record, "city", "location", default="")).strip().lower()
        years = parse_years(pick(record, "experience", "years_experience", "experience_years"))
        keys.add(f"n:{name}|{city}|{round(years) if years is not None else ''}")
    return keys
