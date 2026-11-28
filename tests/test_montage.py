"""Run with python -m unittest discover -s tests (requires FFmpeg and FFprobe)."""
from array import array
from pathlib import Path
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

import quickclip as qc


def quiet(_message):
    pass


class TimelineTests(unittest.TestCase):
    def test_grouped_kills_and_clamped_windows(self):
        self.assertEqual(qc.multi_kill_windows([(1, 1), (1, 1)], 15), [])
        for count in (2, 3, 4):
            kills = [(float(i), 1) for i in range(count)]
            self.assertEqual(qc.multi_kill_windows(kills, 15), [(0, count - 1 + .85)])
        kills = [(0, 1), (2.5, 1), (5, 1), (7.5, 1), (13, 2), (14.9, 1)]
        self.assertEqual(qc.multi_kill_windows(kills, 15), [(0, 8.35), (12.5, 15)])
        self.assertEqual(qc.speed_parts(5, [(3, 6), (-1, 2), (1, 4)], .5), [(0, 5, .5)])

    def test_duplicate_detector_cues_and_distinct_feed_lines(self):
        clip = qc.Clip(10, 20, (), ((12, "Kill feed"), (12, "Kill feed"),
                                  (12.25, "KILL badge"), (12.5, "Kill feed")))
        self.assertEqual(qc.clip_kills(clip), [(2, 1), (2.5, 1)])

    def test_random_seed(self):
        order = qc.montage_transitions("Random", 12, 103)
        self.assertEqual(order, qc.montage_transitions("Random", 12, 103))
        self.assertEqual(order[:6], order[6:])
        self.assertEqual(set(order), set(qc.TRANSITIONS[1:-1]))

    def test_video_and_audio_arguments_stay_scoped(self):
        source = Path(r"C:\Replay Folder\Battlefield (1).mkv")
        output = Path(r"C:\Output Folder\tmp clip.mkv")
        with patch.object(qc, "run_export") as encode:
            qc.encode_video(["ffmpeg"], ["-i", str(source), *qc.FLAC_ARGS], output,
                            quiet, qc.VideoAcceleration(decode=True, encode=True), 2)
        argv = encode.call_args.args[0]
        self.assertEqual(argv[argv.index("-i") + 1], str(source))
        self.assertEqual(argv[-1], str(output))
        self.assertEqual(argv[argv.index("-c:a") + 1], "flac")
        self.assertIn("-frame_size:a", argv)
        for flag in ("-usage", "-quality", "-rc", "-qp_i", "-qp_p", "-qp_b"):
            self.assertIn(flag + ":v", argv)
            self.assertNotIn(flag, argv)


class MontageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="quickclip-qa-", dir=os.environ.get("QUICKCLIP_QA_TMP"))
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.a = cls.root / "Replay A (10-bit).mkv"
        cls.b = cls.root / "Replay B.mkv"
        cls.make_recording(cls.a, 4, "30", "160x96", "yuv420p10le",
                           ["44100", "48000", "32000"], hdr=True)
        cls.make_recording(cls.b, 3, "24", "128x72", "yuv420p", ["48000"], stereo=True)

    @classmethod
    def make_recording(cls, path, duration, rate, size, pixel_format, sample_rates, *, hdr=False, stereo=False):
        command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                   f"testsrc2=s={size}:r={rate}:d={duration}"]
        for index, sample_rate in enumerate(sample_rates):
            command += ["-f", "lavfi", "-i",
                        f"sine=f={220 * (index + 1)}:r={sample_rate}:d={duration}"]
        command += ["-map", "0:v"]
        for index in range(len(sample_rates)):
            command += ["-map", f"{index + 1}:a", f"-metadata:s:a:{index}",
                        f"title={('Game', 'Mic', 'Chat')[index]}"]
        if hdr:
            command += ["-vf", "setparams=range=limited:color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc",
                        "-x264-params", "colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc"]
        qc.run(command + ["-c:v", "libx264", "-threads:v", "2", "-pix_fmt", pixel_format,
                          "-c:a", "pcm_s16le", "-ac:a", "2" if stereo else "1", str(path)])

    def candidates(self, offset=0):
        return [
            qc.BatchResult(self.a, [qc.Clip(.2 + offset, 3.7, (), ((1, "Kill feed"), (2.5, "Kill feed")))], [], []),
            qc.BatchResult(self.b, [qc.Clip(.1, 2.8, (), ((1, "Kill feed"),))], [], []),
        ]

    def test_flac_block_size_and_three_tracks(self):
        command = ["ffmpeg", "-v", "error", "-i", str(self.a), "-map", "0:v", "-map", "0:a",
                   "-c:v", "copy", "-c:a", "flac", "-frame_size:a", "8", "-f", "null", "-"]
        with self.assertRaisesRegex(RuntimeError, "invalid block size: 8"):
            qc.run(command)
        dest = qc.export_clip(self.a, qc.Clip(.2, 3.7, ()), self.root / "Multitrack output", quiet)
        layout, _ = qc.validate_montage_media(dest, 3, 3.5)
        self.assertEqual([s["sample_rate"] for s in layout[1:]], ["44100", "48000", "32000"])

    def test_empty_tracks_fail_even_with_headers(self):
        dest = self.root / "empty.mkv"
        command = ["ffmpeg", "-v", "error", "-i", str(self.a), "-ss", "1", "-map", "0:v",
                   "-map", "0:a", "-af", "atrim=end_sample=0", "-c:v", "libx264", *qc.FLAC_ARGS]
        qc.run(command + [str(dest)])
        with self.assertRaisesRegex(ValueError, "no timestamped packets"):
            qc.validate_montage_media(dest, 3, 3)
        with self.assertRaisesRegex(RuntimeError, "FFmpeg argv"):
            bad = command + ["-frame_size:a", "8", "-y", str(dest)]
            qc.run_export(bad, "Synthetic invalid FLAC block size", quiet)

    def test_all_transitions(self):
        for transition in qc.TRANSITIONS:
            with self.subTest(transition=transition):
                candidates = self.candidates()
                output = self.root / transition
                style = qc.MontageStyle(transition=transition, slowmo_multikills=True)
                results, montage = qc.write_montage([self.a, self.b], candidates, output, quiet, style=style,
                                                    mic_mix_track=2)
                self.assertEqual([r.errors for r in results], [[], []])
                # At 30 fps, 85 source frames fall in the slow-motion window.
                # Twelve are eased at 0.75x and the other 73 play at 0.5x.
                expected_frames = 105 + 81 + (16 + 146 - 85) - (12 if transition != "None" else 0)
                expected = expected_frames / 30
                layout, _ = qc.validate_montage_media(montage, 1, expected, tolerance=.16)
                self.assertEqual(layout[0]["pix_fmt"], "yuv420p")
                self.assertEqual(layout[1]["sample_rate"], "48000")
                details = json.loads(qc.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                             "-count_frames", "-show_entries", "stream=nb_read_frames,color_transfer",
                                             "-of", "json", str(montage)]))["streams"][0]
                self.assertEqual(details["color_transfer"], "bt709")
                self.assertEqual(int(details["nb_read_frames"]), expected_frames)

    def test_audio_marker_follows_video_through_full_clip_slowmo(self):
        source = self.root / "AV sync markers.mkv"
        qc.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=black:s=64x64:r=30:d=2",
                "-f", "lavfi", "-i",
                r"aevalsrc=if(between(t\,0.8\,1.2)\,.5*sin(2*PI*440*t)\,0):s=48000:d=2",
                "-vf", "drawbox=color=white:t=fill:enable='between(t,0.8,1.2)'",
                "-c:v", "libx264", "-threads:v", "2", "-c:a", "pcm_s16le", str(source)])
        clip = qc.Clip(0, 2, (), ((0, "Kills: 2"), (1.8, "Kill feed")))
        _, montage = qc.write_montage([source], [qc.BatchResult(source, [clip], [], [])],
                                      self.root / "AV sync output", quiet,
                                      style=qc.MontageStyle(slowmo_multikills=True))
        qc.validate_montage_media(montage, 1, 4, video_frames=120)
        base = ["ffmpeg", "-v", "error", "-i", str(montage)]
        video = qc.run(base + ["-an", "-vf", "scale=2:2,format=gray", "-f", "rawvideo", "-"])
        audio = array("h", qc.run(base + ["-vn", "-ac", "1", "-f", "s16le", "-"]))
        flash = next(index for index, value in enumerate(video[::4]) if value > 180) / 30
        beep = next(index for index, value in enumerate(audio) if abs(value) > 1000) / 48000
        self.assertAlmostEqual(flash, 1.6, delta=.04)
        self.assertAlmostEqual(beep, flash, delta=2 / 30)

    def test_styled_single_track_keeps_game_audio_when_mic_slot_matches(self):
        clip = qc.Clip(0, 2, ())
        _, montage = qc.write_montage([self.b], [qc.BatchResult(self.b, [clip], [], [])],
                                      self.root / "Single game track", quiet, mic_mix_track=1,
                                      style=qc.MontageStyle(mic_at_kills=True))
        samples = array("h", qc.run(["ffmpeg", "-v", "error", "-i", str(montage), "-vn", "-ac", "1",
                                     "-f", "s16le", "-"]))
        self.assertGreater(max(abs(value) for value in samples), 1000)

    def test_mixed_recordings_keep_all_original_tracks(self):
        output = self.root / "Mixed tracks"
        results, montage = qc.write_montage([self.a, self.b], self.candidates(), output, quiet,
                                            output_format="mp4", mic_mix_track=2)
        self.assertEqual([r.errors for r in results], [[], []])
        layout, _ = qc.validate_montage_media(montage, 4, 6.2)
        self.assertEqual([s["sample_rate"] for s in layout[1:]], ["48000"] * 4)

    def test_slowmo_setting_invalidates_only_affected_pieces(self):
        output = self.root / "Style edit"
        sources = [self.a, self.b]
        original = qc.run_export

        def interrupt(command, description, progress, duration=None):
            if "Joining" in description:
                raise qc.MontagePaused
            original(command, description, progress, duration)

        style = qc.MontageStyle(transition="Whip pan", slowmo_multikills=True)
        previous = qc.save_montage_job(sources, self.candidates(), output, 0, style=style)
        with patch.object(qc, "run_export", side_effect=interrupt):
            with self.assertRaises(qc.MontagePaused):
                qc.resume_montage_job(previous, output, quiet)
        changed = qc.MontageStyle(transition="Whip pan", slowmo_multikills=True, slowmo_speed=.25)
        job = qc.save_montage_job(sources, self.candidates(), output, 0, style=changed)
        self.assertNotEqual(job, previous)
        logs = []
        _, final = qc.resume_montage_job(job, output, logs.append)
        self.assertTrue(final.is_file())
        self.assertTrue(previous.is_file())
        self.assertEqual(sum("Reusing encoded clip." in line for line in logs), 2)
        self.assertEqual(sum("Reusing encoded piece." in line for line in logs), 1)
        self.assertTrue(any("Styling clip 2/2" in line and "Reusing" in line for line in logs))

    def test_resume_regenerates_only_invalid_media(self):
        output = self.root / "Resume"
        job = qc.save_montage_job([self.a, self.b], self.candidates(), output, 0,
                                  style=qc.MontageStyle(transition="Random"))
        stop = threading.Event()
        original = qc.run_export

        def interrupt(command, description, progress, duration=None):
            if "Joining" in description:
                raise qc.MontagePaused
            original(command, description, progress, duration)

        with patch.object(qc, "run_export", side_effect=interrupt):
            with self.assertRaises(qc.MontagePaused):
                qc.resume_montage_job(job, output, quiet, stop=stop)
        paths = sorted(job.parent.glob("segment-*.mkv"))
        paths[0].write_bytes(b"interrupted write")
        plan = next(job.parent.glob("transitions-*.json"))
        mtime = plan.stat().st_mtime_ns
        messages = []
        with patch.object(qc, "export_clip", wraps=qc.export_clip) as export:
            results, final = qc.resume_montage_job(job, output, messages.append)
        self.assertEqual([r.errors for r in results], [[], []])
        self.assertEqual(export.call_count, 1)
        self.assertEqual(export.call_args.args[0], self.a)
        self.assertEqual(plan.stat().st_mtime_ns, mtime)
        self.assertTrue(final.is_file())
        self.assertFalse(job.exists())

    def test_failed_clip_prevents_partial_montage(self):
        output = self.root / "Failure"
        original = qc.export_clip

        def fail(source, *args, **kwargs):
            if source == self.a:
                raise RuntimeError("FLAC encoder failed")
            return original(source, *args, **kwargs)

        with patch.object(qc, "export_clip", side_effect=fail):
            results, final = qc.write_montage([self.a, self.b], self.candidates(), output, quiet)
        self.assertIsNone(final)
        self.assertIn("Replay A (10-bit).mkv", results[0].errors[0])
        self.assertIn("0.20s", results[0].errors[0])
        self.assertFalse(list(output.glob("kill-montage*")))


if __name__ == "__main__":
    unittest.main()
