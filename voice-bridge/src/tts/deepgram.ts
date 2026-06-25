/**
 * Deepgram TTS (Text-to-Speech) provider.
 *
 * Uses Deepgram's TTS API to generate speech audio.
 * Deepgram TTS is fast and supports multiple voices.
 */

import { Logger } from 'pino';
import { convertToTwilioAudio } from '../audio.js';

export interface DeepgramTTSOptions {
  apiKey: string;
  voice?: string;
  model?: string;
  logger?: Logger;
}

export class DeepgramTTS {
  private apiKey: string;
  private voice: string;
  private model: string;
  private logger?: Logger;

  constructor(options: DeepgramTTSOptions) {
    this.apiKey = options.apiKey;
    this.voice = options.voice || 'aura-asteria-en';
    this.model = options.model || 'aura';
    this.logger = options.logger;
  }

  /**
   * Generate speech audio from text.
   *
   * @param text Text to convert to speech
   * @returns PCM audio buffer (24kHz, 16-bit, mono)
   */
  async synthesize(text: string): Promise<Buffer> {
    this.logger?.debug({ text: text.substring(0, 50) }, 'Synthesizing speech with Deepgram');

    const response = await fetch(
      `https://api.deepgram.com/v1/speak?model=${this.voice}&encoding=linear16&sample_rate=24000`,
      {
        method: 'POST',
        headers: {
          'Authorization': `Token ${this.apiKey}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ text }),
      }
    );

    if (!response.ok) {
      const error = await response.text();
      throw new Error(`Deepgram TTS error: ${response.status} - ${error}`);
    }

    const arrayBuffer = await response.arrayBuffer();
    const pcmBuffer = Buffer.from(arrayBuffer);

    // Add 200ms of silence at the end to prevent audio cutoff artifacts
    const silencePadding = Buffer.alloc(24000 * 2 * 0.2); // 200ms at 24kHz 16-bit
    const paddedBuffer = Buffer.concat([pcmBuffer, silencePadding]);

    this.logger?.debug({ bytes: paddedBuffer.length }, 'Speech synthesized with Deepgram');

    return paddedBuffer;
  }

  /**
   * Generate speech audio and convert to Twilio mu-law format.
   *
   * @param text Text to convert to speech
   * @returns Generator yielding mu-law audio frames (160 bytes each)
   */
  async* synthesizeForTwilio(text: string): AsyncGenerator<Buffer> {
    const pcmBuffer = await this.synthesize(text);

    // Deepgram outputs 24kHz PCM (we requested it)
    for (const frame of convertToTwilioAudio(pcmBuffer, 24000)) {
      yield frame;
    }
  }

  /**
   * Generate all audio frames at once for Twilio.
   *
   * @param text Text to convert to speech
   * @returns Array of mu-law audio frames
   */
  async synthesizeAllForTwilio(text: string): Promise<Buffer[]> {
    const frames: Buffer[] = [];
    for await (const frame of this.synthesizeForTwilio(text)) {
      frames.push(frame);
    }
    return frames;
  }
}

/**
 * Create a Deepgram TTS instance.
 */
export function createDeepgramTTS(options: DeepgramTTSOptions): DeepgramTTS {
  return new DeepgramTTS(options);
}

// Available Deepgram Aura voices
export const DEEPGRAM_VOICES = [
  'aura-asteria-en',    // Female, American English
  'aura-luna-en',       // Female, American English
  'aura-stella-en',     // Female, American English
  'aura-athena-en',     // Female, British English
  'aura-hera-en',       // Female, American English
  'aura-orion-en',      // Male, American English
  'aura-arcas-en',      // Male, American English
  'aura-perseus-en',    // Male, American English
  'aura-angus-en',      // Male, Irish English
  'aura-orpheus-en',    // Male, American English
  'aura-helios-en',     // Male, British English
  'aura-zeus-en',       // Male, American English
] as const;

export type DeepgramVoice = typeof DEEPGRAM_VOICES[number];
