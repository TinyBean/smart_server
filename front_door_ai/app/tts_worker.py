"""Piper runs in a cancellable subprocess, separate from HTTP and vision workers."""
import io
import json
import math
import sys
import wave

import numpy as np
from scipy.signal import resample_poly


def main():
    from piper import PiperVoice
    from piper.config import SynthesisConfig
    voice = PiperVoice.load(sys.argv[1])
    options = json.load(sys.stdin)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        voice.synthesize_wav(options["text"], output, syn_config=SynthesisConfig(length_scale=1.0))
    buffer.seek(0)
    with wave.open(buffer, "rb") as source:
        rate, channels, width = source.getframerate(), source.getnchannels(), source.getsampwidth()
        if width != 2:
            raise ValueError("Piper must produce PCM16")
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32)
        if channels != 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
    divisor = math.gcd(rate, 16000)
    if rate != 16000:
        samples = resample_poly(samples, 16000//divisor, rate//divisor)
    samples = np.clip(np.rint(samples), -32768, 32767).astype("<i2")
    with wave.open(sys.argv[2], "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(samples.tobytes())


if __name__ == "__main__":
    main()
