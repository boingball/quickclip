"""Persist the most recent desktop montage scan, one replay at a time."""
from collections import Counter
from dataclasses import asdict
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile, mkdtemp

from quickclip import BatchResult, Clip, KillFeedOptions, SkullReference, WeaponReference


def write_json(path, data):
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def source_stamp(path):
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return [info.st_size, info.st_mtime_ns]


class SavedScan:
    def __init__(self, directory, plan):
        self.directory = directory
        self.input_path = Path(plan["input"])
        self.sources = [Path(source) for source in plan["sources"]]
        self.indices = {source: index for index, source in enumerate(self.sources)}
        self.player = plan["player"]
        self.before, self.after = plan["before"], plan["after"]
        options = dict(plan["feed"])
        options["region"] = tuple(options["region"])
        reference = options.get("skull_reference")
        if reference is not None:
            options["skull_reference"] = SkullReference(tuple(reference["box"]), tuple(reference["pixels"]))
        options["weapon_references"] = tuple(
            WeaponReference(item["label"], item["mode"], tuple(item["box"]), tuple(item["pixels"]))
            for item in options.get("weapon_references", ())
        )
        self.feed = KillFeedOptions(**options)
        self.results = {}
        self.rejections = {}
        self.stamps = {}

    @classmethod
    def create(cls, root, input_path, sources, player, before, after, feed):
        root.mkdir(parents=True, exist_ok=True)
        directory = Path(mkdtemp(prefix="scan-", dir=root))
        plan = {
            "version": 1, "directory": directory.name, "input": str(input_path),
            "sources": [str(source) for source in sources], "player": player,
            "before": before, "after": after, "feed": asdict(feed),
        }
        scan = cls(directory, plan)
        write_json(root / "latest.json", plan)
        return scan

    @classmethod
    def load(cls, root):
        try:
            plan = json.loads((root / "latest.json").read_text(encoding="utf-8"))
            name = plan["directory"]
            if plan["version"] != 1 or Path(name).name != name or not name.startswith("scan-"):
                return None
            scan = cls(root / name, plan)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        for source in scan.sources:
            try:
                stored = json.loads(scan.result_path(source).read_text(encoding="utf-8"))
                if stored["stamp"] != source_stamp(source):
                    continue
                row = stored["result"]
                row["source"] = source
                row["clips"] = [Clip(item["start"], item["end"], tuple(item["reasons"])) for item in row["clips"]]
                row["exports"] = []
                for key in ("feed_rows", "feed_lookalikes"):
                    row[key] = Counter(row[key])
                scan.results[source] = BatchResult(**row)
                scan.rejections[source] = set(stored["rejected"])
                scan.stamps[source] = stored["stamp"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return scan

    def result_path(self, source):
        return self.directory / f"{self.indices[source]:06d}.json"

    def pending(self):
        return [source for source in self.sources if source not in self.results]

    def save_result(self, result):
        self.stamps[result.source] = source_stamp(result.source)
        self.results[result.source] = result
        self.rejections[result.source] = set()
        self.write_result(result.source)

    def update_choice(self, source, clip, keep):
        index = self.results[source].clips.index(clip)
        rejected = self.rejections[source]
        if keep:
            rejected.discard(index)
        else:
            rejected.add(index)
        self.write_result(source)

    def write_result(self, source):
        result = self.results[source]
        row = vars(result).copy()
        row["source"] = str(source)
        row["clips"] = [asdict(clip) for clip in result.clips]
        row["exports"] = []
        write_json(self.result_path(source), {
            "stamp": self.stamps[source], "result": row, "rejected": sorted(self.rejections[source]),
        })
