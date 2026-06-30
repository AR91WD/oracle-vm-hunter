from dataclasses import dataclass
from typing import List, Optional, Dict, Any


# =========================
# SEGMENT (основа Whisper)
# =========================
@dataclass
class Segment:
    start: float
    end: float
    text: str
    language: Optional[str] = None
    confidence: Optional[float] = None


# =========================
# FULL TRANSCRIPTION RESULT
# =========================
@dataclass
class TranscriptionResult:
    text: str
    segments: List[Segment]
    language: Optional[str] = None
    duration: Optional[float] = None
    source_file: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "language": self.language,
            "duration": self.duration,
            "source_file": self.source_file,
            "segments": [
                {
                    "start": s.start,
                    "end": s.end,
                    "text": s.text,
                    "language": s.language,
                    "confidence": s.confidence,
                }
                for s in self.segments
            ],
        }


# =========================
# EXPORT FORMAT TYPES
# =========================
@dataclass
class ExportConfig:
    format: str  # txt, srt, json, vtt
    include_timestamps: bool = True
    include_confidence: bool = False
    language_filter: Optional[List[str]] = None


# =========================
# AUDIO META
# =========================
@dataclass
class AudioMeta:
    path: str
    sample_rate: Optional[int] = None
    duration: Optional[float] = None
