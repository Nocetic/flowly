/**
 * OpenAI Whisper STT (Speech-to-Text) provider.
 *
 * Uses OpenAI's Whisper API for transcription.
 * Note: This is not real-time - audio is buffered and sent in batches.
 */

import OpenAI from 'openai';
import { EventEmitter } from 'events';
import { Logger } from 'pino';
import { wrapInWav } from '../audio.js';
import type { STTResult } from '../types.js';

export interface OpenAISTTOptions {
  apiKey: string;
  language?: string;
  model?: string;
  bufferDurationMs?: number;
  logger?: Logger;
}

export class OpenAISTT extends EventEmitter {
  private client: OpenAI;
  private logger?: Logger;
  private options: OpenAISTTOptions;
  private audioBuffer: Buffer[] = [];
  private bufferTimer: NodeJS.Timeout | null = null;
  private isProcessing = false;

  constructor(options: OpenAISTTOptions) {
    super();
    this.options = options;
    this.logger = options.logger;
    this.client = new OpenAI({ apiKey: options.apiKey });
  }

  /**
   * Connect (no-op for OpenAI, but maintains interface compatibility).
   */
  async connect(): Promise<void> {
    this.logger?.info('OpenAI STT ready');
    this.emit('connected');
  }

  /**
   * Send audio data for transcription.
   * Audio is buffered and sent in batches.
   * Expects 16-bit PCM at 16kHz mono.
   */
  send(audioData: Buffer): void {
    this.audioBuffer.push(audioData);

    // Reset buffer timer
    if (this.bufferTimer) {
      clearTimeout(this.bufferTimer);
    }

    // Process after silence (configurable duration)
    const bufferDuration = this.options.bufferDurationMs || 1500;
    this.bufferTimer = setTimeout(() => {
      this.processBuffer();
    }, bufferDuration);
  }

  /**
   * Process buffered audio.
   */
  private async processBuffer(): Promise<void> {
    if (this.audioBuffer.length === 0 || this.isProcessing) {
      return;
    }

    this.isProcessing = true;
    const audioData = Buffer.concat(this.audioBuffer);
    this.audioBuffer = [];

    try {
      // Minimum audio length check (0.1 seconds at 16kHz = 3200 bytes)
      if (audioData.length < 3200) {
        this.logger?.debug('Audio too short, skipping');
        this.isProcessing = false;
        return;
      }

      this.logger?.debug({ bytes: audioData.length }, 'Processing audio buffer');

      // Wrap in WAV format
      const wavData = wrapInWav(audioData, 16000, 1);

      // Create a File-like object for the API
      const file = new File([wavData], 'audio.wav', { type: 'audio/wav' });

      // Call Whisper API
      const response = await this.client.audio.transcriptions.create({
        file,
        model: this.options.model || 'whisper-1',
        language: this.options.language?.split('-')[0], // OpenAI uses 'en' not 'en-US'
        response_format: 'verbose_json',
      });

      const result: STTResult = {
        text: response.text || '',
        confidence: 1.0, // Whisper doesn't provide confidence
        isFinal: true,
        words: (response as any).words?.map((w: any) => ({
          word: w.word,
          start: w.start,
          end: w.end,
          confidence: 1.0,
        })),
      };

      if (result.text) {
        this.logger?.debug({ result }, 'Transcript received');
        this.emit('transcript', result);
        this.emit('final_transcript', result);
      }
    } catch (error) {
      this.logger?.error({ error }, 'OpenAI STT error');
      this.emit('error', error);
    } finally {
      this.isProcessing = false;
    }
  }

  /**
   * Signal end of audio stream and process remaining buffer.
   */
  async finalize(): Promise<void> {
    if (this.bufferTimer) {
      clearTimeout(this.bufferTimer);
      this.bufferTimer = null;
    }

    await this.processBuffer();
  }

  /**
   * Disconnect (clear buffers).
   */
  disconnect(): void {
    if (this.bufferTimer) {
      clearTimeout(this.bufferTimer);
      this.bufferTimer = null;
    }
    this.audioBuffer = [];
    this.isProcessing = false;
  }

  /**
   * Check if ready (always true for OpenAI).
   */
  get connected(): boolean {
    return true;
  }
}

/**
 * Create an OpenAI STT instance.
 */
export function createOpenAISTT(options: OpenAISTTOptions): OpenAISTT {
  return new OpenAISTT(options);
}
