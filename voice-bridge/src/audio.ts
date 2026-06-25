/**
 * Audio conversion utilities for Twilio voice calls.
 *
 * Twilio uses mu-law (G.711) encoding at 8kHz for telephony audio.
 * This module provides conversion between PCM and mu-law formats.
 */

// Twilio audio parameters
export const TWILIO_SAMPLE_RATE = 8000; // 8kHz
export const TWILIO_CHANNELS = 1; // Mono
export const TWILIO_FRAME_SIZE = 160; // 20ms at 8kHz
export const TWILIO_BYTES_PER_FRAME = 160; // 160 bytes of mu-law = 20ms

// Mu-law encoding table (ITU-T G.711)
const MU_LAW_ENCODE_TABLE = new Uint8Array([
  0, 0, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3,
  4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4,
  5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5,
  5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5,
  6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
  6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
  6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
  6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
  7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
  7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
  7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
  7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
  7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
  7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
  7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
  7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
]);

// Mu-law decoding table
const MU_LAW_DECODE_TABLE = new Int16Array([
  -32124, -31100, -30076, -29052, -28028, -27004, -25980, -24956,
  -23932, -22908, -21884, -20860, -19836, -18812, -17788, -16764,
  -15996, -15484, -14972, -14460, -13948, -13436, -12924, -12412,
  -11900, -11388, -10876, -10364, -9852, -9340, -8828, -8316,
  -7932, -7676, -7420, -7164, -6908, -6652, -6396, -6140,
  -5884, -5628, -5372, -5116, -4860, -4604, -4348, -4092,
  -3900, -3772, -3644, -3516, -3388, -3260, -3132, -3004,
  -2876, -2748, -2620, -2492, -2364, -2236, -2108, -1980,
  -1884, -1820, -1756, -1692, -1628, -1564, -1500, -1436,
  -1372, -1308, -1244, -1180, -1116, -1052, -988, -924,
  -876, -844, -812, -780, -748, -716, -684, -652,
  -620, -588, -556, -524, -492, -460, -428, -396,
  -372, -356, -340, -324, -308, -292, -276, -260,
  -244, -228, -212, -196, -180, -164, -148, -132,
  -120, -112, -104, -96, -88, -80, -72, -64,
  -56, -48, -40, -32, -24, -16, -8, 0,
  32124, 31100, 30076, 29052, 28028, 27004, 25980, 24956,
  23932, 22908, 21884, 20860, 19836, 18812, 17788, 16764,
  15996, 15484, 14972, 14460, 13948, 13436, 12924, 12412,
  11900, 11388, 10876, 10364, 9852, 9340, 8828, 8316,
  7932, 7676, 7420, 7164, 6908, 6652, 6396, 6140,
  5884, 5628, 5372, 5116, 4860, 4604, 4348, 4092,
  3900, 3772, 3644, 3516, 3388, 3260, 3132, 3004,
  2876, 2748, 2620, 2492, 2364, 2236, 2108, 1980,
  1884, 1820, 1756, 1692, 1628, 1564, 1500, 1436,
  1372, 1308, 1244, 1180, 1116, 1052, 988, 924,
  876, 844, 812, 780, 748, 716, 684, 652,
  620, 588, 556, 524, 492, 460, 428, 396,
  372, 356, 340, 324, 308, 292, 276, 260,
  244, 228, 212, 196, 180, 164, 148, 132,
  120, 112, 104, 96, 88, 80, 72, 64,
  56, 48, 40, 32, 24, 16, 8, 0,
]);

/**
 * Convert a single 16-bit PCM sample to mu-law.
 */
export function pcmToMulaw(pcmSample: number): number {
  // Clamp to 16-bit range
  pcmSample = Math.max(-32768, Math.min(32767, pcmSample));

  // Get sign and absolute value
  const sign = pcmSample < 0 ? 0x80 : 0x00;
  if (pcmSample < 0) {
    pcmSample = -pcmSample;
  }

  // Add bias and clamp
  pcmSample += 0x84;
  if (pcmSample > 0x7fff) {
    pcmSample = 0x7fff;
  }

  // Get exponent and mantissa
  const exponent = MU_LAW_ENCODE_TABLE[(pcmSample >> 8) & 0xff];
  const mantissa = (pcmSample >> (exponent + 3)) & 0x0f;

  // Combine and invert (mu-law uses inverted bits)
  return ~(sign | (exponent << 4) | mantissa) & 0xff;
}

/**
 * Convert a single mu-law sample to 16-bit PCM.
 */
export function mulawToPcm(mulawSample: number): number {
  return MU_LAW_DECODE_TABLE[mulawSample];
}

/**
 * Convert PCM Int16 buffer to mu-law Uint8 buffer.
 */
export function pcmBufferToMulaw(pcmBuffer: Int16Array): Uint8Array {
  const mulawBuffer = new Uint8Array(pcmBuffer.length);
  for (let i = 0; i < pcmBuffer.length; i++) {
    mulawBuffer[i] = pcmToMulaw(pcmBuffer[i]);
  }
  return mulawBuffer;
}

/**
 * Convert mu-law Uint8 buffer to PCM Int16 buffer.
 */
export function mulawBufferToPcm(mulawBuffer: Uint8Array): Int16Array {
  const pcmBuffer = new Int16Array(mulawBuffer.length);
  for (let i = 0; i < mulawBuffer.length; i++) {
    pcmBuffer[i] = mulawToPcm(mulawBuffer[i]);
  }
  return pcmBuffer;
}

/**
 * Convert raw PCM bytes (16-bit little-endian) to mu-law bytes.
 */
export function pcmBytesToMulaw(pcmBytes: Buffer): Buffer {
  const numSamples = pcmBytes.length / 2;
  const mulawBytes = Buffer.alloc(numSamples);

  for (let i = 0; i < numSamples; i++) {
    const pcmSample = pcmBytes.readInt16LE(i * 2);
    mulawBytes[i] = pcmToMulaw(pcmSample);
  }

  return mulawBytes;
}

/**
 * Convert mu-law bytes to raw PCM bytes (16-bit little-endian).
 */
export function mulawBytesToPcm(mulawBytes: Buffer): Buffer {
  const pcmBytes = Buffer.alloc(mulawBytes.length * 2);

  for (let i = 0; i < mulawBytes.length; i++) {
    const pcmSample = mulawToPcm(mulawBytes[i]);
    pcmBytes.writeInt16LE(pcmSample, i * 2);
  }

  return pcmBytes;
}

/**
 * Simple linear resampling.
 * For better quality, use a proper resampling library.
 */
export function resampleLinear(
  pcmBuffer: Int16Array,
  fromRate: number,
  toRate: number
): Int16Array {
  if (fromRate === toRate) {
    return pcmBuffer;
  }

  const ratio = toRate / fromRate;
  const newLength = Math.floor(pcmBuffer.length * ratio);
  const result = new Int16Array(newLength);

  for (let i = 0; i < newLength; i++) {
    const srcIdx = i / ratio;
    const idx0 = Math.floor(srcIdx);
    const idx1 = Math.min(idx0 + 1, pcmBuffer.length - 1);
    const frac = srcIdx - idx0;

    result[i] = Math.round(pcmBuffer[idx0] * (1 - frac) + pcmBuffer[idx1] * frac);
  }

  return result;
}

/**
 * Split audio buffer into fixed-size chunks.
 */
export function* chunkAudio(audio: Buffer, chunkSize: number): Generator<Buffer> {
  for (let i = 0; i < audio.length; i += chunkSize) {
    let chunk = audio.subarray(i, i + chunkSize);
    // Pad last chunk if needed
    if (chunk.length < chunkSize) {
      const padded = Buffer.alloc(chunkSize);
      chunk.copy(padded);
      chunk = padded;
    }
    yield Buffer.from(chunk);
  }
}

/**
 * Convert 24kHz PCM (OpenAI TTS output) to Twilio-compatible mu-law frames.
 */
export function* convertToTwilioAudio(
  pcmBuffer: Buffer,
  sampleRate: number = 24000
): Generator<Buffer> {
  // Convert to Int16Array
  const numSamples = pcmBuffer.length / 2;
  const pcmInt16 = new Int16Array(numSamples);
  for (let i = 0; i < numSamples; i++) {
    pcmInt16[i] = pcmBuffer.readInt16LE(i * 2);
  }

  // Resample to 8kHz if needed
  const resampled = sampleRate !== TWILIO_SAMPLE_RATE
    ? resampleLinear(pcmInt16, sampleRate, TWILIO_SAMPLE_RATE)
    : pcmInt16;

  // Convert to mu-law
  const mulaw = pcmBufferToMulaw(resampled);

  // Split into 20ms frames (160 bytes each)
  yield* chunkAudio(Buffer.from(mulaw), TWILIO_BYTES_PER_FRAME);
}

/**
 * Convert Twilio mu-law frames to PCM for STT.
 */
export function convertFromTwilioAudio(
  mulawFrames: Buffer[],
  targetRate: number = 16000
): Buffer {
  // Combine frames
  const mulaw = Buffer.concat(mulawFrames);

  // Convert to PCM
  const pcmInt16 = mulawBufferToPcm(mulaw);

  // Resample if needed
  const resampled = targetRate !== TWILIO_SAMPLE_RATE
    ? resampleLinear(pcmInt16, TWILIO_SAMPLE_RATE, targetRate)
    : pcmInt16;

  // Convert to Buffer
  const pcmBuffer = Buffer.alloc(resampled.length * 2);
  for (let i = 0; i < resampled.length; i++) {
    pcmBuffer.writeInt16LE(resampled[i], i * 2);
  }

  return pcmBuffer;
}

/**
 * Create a WAV header for PCM audio.
 */
export function createWavHeader(
  dataLength: number,
  sampleRate: number = 16000,
  channels: number = 1,
  bitsPerSample: number = 16
): Buffer {
  const header = Buffer.alloc(44);
  const byteRate = sampleRate * channels * (bitsPerSample / 8);
  const blockAlign = channels * (bitsPerSample / 8);

  // RIFF header
  header.write('RIFF', 0);
  header.writeUInt32LE(dataLength + 36, 4);
  header.write('WAVE', 8);

  // fmt chunk
  header.write('fmt ', 12);
  header.writeUInt32LE(16, 16); // Subchunk size
  header.writeUInt16LE(1, 20); // Audio format (PCM)
  header.writeUInt16LE(channels, 22);
  header.writeUInt32LE(sampleRate, 24);
  header.writeUInt32LE(byteRate, 28);
  header.writeUInt16LE(blockAlign, 32);
  header.writeUInt16LE(bitsPerSample, 34);

  // data chunk
  header.write('data', 36);
  header.writeUInt32LE(dataLength, 40);

  return header;
}

/**
 * Wrap PCM data in a WAV container.
 */
export function wrapInWav(
  pcmData: Buffer,
  sampleRate: number = 16000,
  channels: number = 1
): Buffer {
  const header = createWavHeader(pcmData.length, sampleRate, channels);
  return Buffer.concat([header, pcmData]);
}
