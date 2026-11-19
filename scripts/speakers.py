"""Speaker turns (WORK/speakers.json from diarize.py) -> who said each word, and the names to show.

Used by cut_clips.py (a new cue at every change of speaker, one SRT per speaker) and diarize.py.

Names come from, in order of precedence:
  WORK/speaker_map.json   {"SPEAKER_00": "小明", "SPEAKER_03": "merge:SPEAKER_01", "SPEAKER_04": "ignore"}
  config "speakers"       {"map": {"SPEAKER_00": "小明"}} or the older {"SPEAKER_00": "小明"}
A label nobody named keeps its raw name (SPEAKER_00). "ignore" is for a cluster that is not a
person (game voices, music): its words go to the unsure file, not to anyone's track.
"""
from bisect import bisect_right
from pathlib import Path

from common import load_json

UNSURE = "?"         # nobody, or an ignored cluster
OVERLAP = "overlap"  # two or more people at once
UNSURE_NAME = "未確定"  # file name part for both
CONFIG_KEYS = ("count", "names", "map")
SHORT_SEC, SHORT_CHARS = 0.4, 3  # an interjection this small between one person's lines is not trusted


def name_map(cfg_speakers, work_map=None):
    """label -> name / "merge:LABEL" / "ignore", from config (both formats) and speaker_map.json."""
    sp = cfg_speakers or {}
    out = {k: v for k, v in sp.items() if k not in CONFIG_KEYS and isinstance(v, str)}  # older format
    out.update({k: v for k, v in (sp.get("map") or {}).items() if isinstance(v, str)})
    out.update({k: v for k, v in (work_map or {}).items() if isinstance(v, str)})
    return out


class Speakers:
    def __init__(self, turns, mapping=None):
        self.turns = sorted(([float(a), float(b), str(s)] for a, b, s in turns), key=lambda t: t[0])
        self.starts = [t[0] for t in self.turns]
        self.longest = max((b - a for a, b, _ in self.turns), default=0.0)
        self.mapping = mapping or {}

    def resolve(self, label):
        """Raw label -> display name, or UNSURE for an ignored cluster. Follows merge: chains."""
        seen = set()
        while label not in seen:
            seen.add(label)
            v = self.mapping.get(label)
            if v is None:
                return label
            v = v.strip()
            if v == "ignore":
                return UNSURE
            if v.startswith("merge:"):
                label = v[len("merge:"):].strip()
                continue
            return v or label
        return label  # a merge loop: stop where it closed

    def at(self, t):
        """Names talking at time t (ignored clusters left out), and whether an ignored one was there."""
        names, ignored = set(), False
        lo = bisect_right(self.starts, t - self.longest)
        for a, b, s in self.turns[max(0, lo - 1):bisect_right(self.starts, t)]:
            if a <= t <= b:
                n = self.resolve(s)
                if n == UNSURE:
                    ignored = True
                else:
                    names.add(n)
        return names, ignored

    def label_words(self, words):
        """One label per word of one transcript segment: a name, OVERLAP or UNSURE.

        Whisper writes down one voice per segment, so the segment is the unit we trust most:
        - a word no turn covers takes the nearest named word of the same segment;
        - an overlapped word goes to the segment's main speaker when that person is one of the
          voices there (another voice laughing or saying "嗯" under them is the usual case);
        - a flip of 1-2 words (< SLIP_SEC) to someone else inside one person's segment is taken as
          diarization jitter and given back. Ignored clusters are never filled or smoothed over."""
        labs, sets, fill = [], [], []
        for w in words:
            names, ignored = self.at((w["start"] + w["end"]) / 2)
            sets.append(names)
            if len(names) == 1:
                labs.append(next(iter(names)))
            elif len(names) > 1:
                labs.append(OVERLAP)
            else:
                labs.append(UNSURE)
                if not ignored:
                    fill.append(len(labs) - 1)
        named = [i for i, x in enumerate(labs) if is_person(x)]
        for i in fill:
            if named:
                mid = (words[i]["start"] + words[i]["end"]) / 2
                j = min(named, key=lambda k: abs((words[k]["start"] + words[k]["end"]) / 2 - mid))
                labs[i] = labs[j]
        counts = {}
        for x in labs:
            if is_person(x):
                counts[x] = counts.get(x, 0) + 1
        if counts:
            main = max(counts, key=counts.get)
            if counts[main] * 2 >= sum(counts.values()):
                labs = [main if x == OVERLAP and main in sets[i] else x for i, x in enumerate(labs)]
        return smooth_slips(labs, words)


SLIP_WORDS, SLIP_SEC = 2, 0.6


def smooth_slips(labs, words):
    """Give a short run of another label back to the person talking on both sides of it."""
    runs, i = [], 0
    while i < len(labs):
        j = i
        while j + 1 < len(labs) and labs[j + 1] == labs[i]:
            j += 1
        runs.append([i, j])
        i = j + 1
    for k in range(1, len(runs) - 1):
        (pa, _), (a, b), (na, _) = runs[k - 1], runs[k], runs[k + 1]
        side = labs[pa]
        if labs[a] != UNSURE and is_person(side) and labs[na] == side != labs[a] and \
                b - a + 1 <= SLIP_WORDS and words[b]["end"] - words[a]["start"] < SLIP_SEC:
            for x in range(a, b + 1):
                labs[x] = side
    return labs


def is_person(label):
    return label not in (UNSURE, OVERLAP, None)


def mark_interjections(cues):
    """cues: [start, end, text, speaker, spoken_seconds]. A tiny cue given to a third person between
    two lines of the same speaker is more often a diarization slip than a real interjection: keep
    it but mark it unsure, so the editor decides."""
    for i in range(1, len(cues) - 1):
        prev, cur, nxt = cues[i - 1][3], cues[i][3], cues[i + 1][3]
        if is_person(cur) and is_person(prev) and prev == nxt != cur and \
                cues[i][4] < SHORT_SEC and len(cues[i][2].replace(" ", "")) <= SHORT_CHARS:
            cues[i][3] = UNSURE
    return cues


def source_matches(meta, source):
    """speakers.json records which stream it came from; refuse one made from another file."""
    if not meta:
        return True  # made before the check existed
    p = Path(source)
    try:
        size = p.stat().st_size
    except OSError:
        return meta.get("name") == p.name
    return meta.get("name") == p.name and meta.get("size") in (None, size)


def load(paths, cfg, source):
    """-> (Speakers or None, message or None)."""
    store = paths["dir"] / "speakers.json"
    if not store.exists():
        return None, None
    data = load_json(store)
    if not source_matches(data.get("source"), source):
        return None, f"speakers.json 是另一個直播檔做的（{data['source'].get('name')}），這次不分軌"
    work_map = paths["dir"] / "speaker_map.json"
    mapping = name_map(cfg.get("speakers"), load_json(work_map) if work_map.exists() else None)
    return Speakers(data.get("turns", []), mapping), None
