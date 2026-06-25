/**
 * OpenAI TTS (Text-to-Speech) provider.
 *
 * Uses OpenAI's TTS API to generate speech audio.
 */

import OpenAI from 'openai';
import { Logger } from 'pino';
import { convertToTwilioAudio } from '../audio.js';

export interface OpenAITTSOptions {
  apiKey: string;
  voice?: string;
  model?: string;
  speed?: number;
  logger?: Logger;
}

export class OpenAITTS {
  private client: OpenAI;
  private logger?: Logger;
  private options: OpenAITTSOptions;

  constructor(options: OpenAITTSOptions) {
    this.options = options;
    this.logger = options.logger;
    this.client = new OpenAI({ apiKey: options.apiKey });
  }

  /**
   * Generate speech audio from text.
   *
   * @param text Text to convert to speech
   * @returns PCM audio buffer (24kHz, 16-bit, mono)
   */
  async synthesize(text: string): Promise<Buffer> {
    this.logger?.debug({ text: text.substring(0, 50) }, 'Synthesizing speech');

    const response = await this.client.audio.speech.create({
      model: this.options.model || 'tts-1',
      voice: (this.options.voice || 'nova') as any,
      input: text,
      response_format: 'pcm', // Raw PCM audio
      speed: this.options.speed || 1.0,
    });

    // Get the audio data as an ArrayBuffer
    const arrayBuffer = await response.arrayBuffer();
    const pcmBuffer = Buffer.from(arrayBuffer);

    this.logger?.debug({ bytes: pcmBuffer.length }, 'Speech synthesized');

    return pcmBuffer;
  }

  /**
   * Generate speech audio and convert to Twilio mu-law format.
   *
   * @param text Text to convert to speech
   * @returns Generator yielding mu-law audio frames (160 bytes each)
   */
  async* synthesizeForTwilio(text: string): AsyncGenerator<Buffer> {
    const pcmBuffer = await this.synthesize(text);

    // OpenAI TTS outputs 24kHz PCM
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
 * Create an OpenAI TTS instance.
 */
export function createOpenAITTS(options: OpenAITTSOptions): OpenAITTS {
  return new OpenAITTS(options);
}

// Available voices
export const OPENAI_VOICES = ['alloy', 'echo', 'fable', 'onyx', 'nova', 'shimmer'] as const;
export type OpenAIVoice = typeof OPENAI_VOICES[number];

// Available models
export const OPENAI_TTS_MODELS = ['tts-1', 'tts-1-hd'] as const;
export type OpenAITTSModel = typeof OPENAI_TTS_MODELS[number];
