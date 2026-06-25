/**
 * Groq Whisper STT (Speech-to-Text) provider.
 *
 * Uses Groq's Whisper API for fast transcription.
 * Note: Groq Whisper is batch-based, not streaming.
 */

import { EventEmitter } from 'events';
import { Logger } from 'pino';
import type { STTResult } from '../types.js';

export interface GroqSTTOptions {
  apiKey: string;
  language?: string;
  model?: string;
  logger?: Logger;
}

export class GroqSTT extends EventEmitter {
  private apiKey: string;
  private language: string;
  private model: string;
  private logger?: Logger;
  public audioBuffer: Buffer[] = [];
  public totalBytes = 0;
  private isConnected = false;
  private silenceTimeout: NodeJS.Timeout | null = null;
  private readonly SILENCE_THRESHOLD_MS = 1500; // Send after 1.5s of silence
  private readonly MAX_BUFFER_BYTES = 160000; // ~5 seconds at 16kHz 16-bit mono

  constructor(options: GroqSTTOptions) {
    super();
    this.apiKey = options.apiKey;
    // Groq Whisper only accepts 2-letter language codes (e.g., 'en', not 'en-US')
    const lang = options.language || 'en';
    this.language = lang.split('-')[0]; // Convert 'en-US' to 'en'
    this.model = options.model || 'whisper-large-v3-turbo';
    this.logger = options.logger;
  }

  /**
   * Connect (initialize) the STT provider.
   */
  async connect(): Promise<void> {
    this.isConnected = true;
    this.logger?.info('Groq STT initialized');
    this.emit('connected');
  }

  /**
   * Send audio data for transcription.
   * Buffers audio and sends when silence is detected or buffer is full.
   */
  send(audioData: Buffer): void {
    if (!this.isConnected) {
      this.logger?.warn('Cannot send audio: not connected');
      return;
    }

    // Add to buffer
    this.audioBuffer.push(audioData);
    this.totalBytes += audioData.length;

    // Log periodically
    if (this.audioBuffer.length % 10 === 1) {
      this.logger?.debug({ chunks: this.audioBuffer.length, totalBytes: this.totalBytes }, 'Audio received by Groq STT');
    }

    // If buffer is full, transcribe immediately
    if (this.totalBytes >= this.MAX_BUFFER_BYTES) {
      this.logger?.debug({ totalBytes: this.totalBytes }, 'Buffer full, transcribing...');
      if (this.silenceTimeout) {
        clearTimeout(this.silenceTimeout);
        this.silenceTimeout = null;
      }
      this.transcribeBuffer();
      return;
    }

    // Reset silence timer
    if (this.silenceTimeout) {
      clearTimeout(this.silenceTimeout);
    }

    // Set timer to transcribe after silence
    this.silenceTimeout = setTimeout(() => {
      this.transcribeBuffer();
    }, this.SILENCE_THRESHOLD_MS);
  }

  /**
   * Transcribe the buffered audio using Groq Whisper API.
   */
  private async transcribeBuffer(): Promise<void> {
    if (this.audioBuffer.length === 0) return;

    // Combine all buffers
    const audioData = Buffer.concat(this.audioBuffer);
    this.audioBuffer = [];
    this.totalBytes = 0;

    this.logger?.info({ bytes: audioData.length }, 'Starting Groq transcription');

    // Skip if too short (less than 0.5 seconds at 16kHz 16-bit)
    if (audioData.length < 16000) {
      this.logger?.debug('Audio too short, skipping transcription');
      return;
    }

    try {
      this.logger?.debug({ bytes: audioData.length }, 'Transcribing audio with Groq');

      // Create WAV header for the PCM data
      const wavBuffer = this.createWavBuffer(audioData);

      // Create form data
      const formData = new FormData();
      const blob = new Blob([wavBuffer], { type: 'audio/wav' });
      formData.append('file', blob, 'audio.wav');
      formData.append('model', this.model);
      formData.append('language', this.language);
      formData.append('response_format', 'json');

      // Call Groq API
      const response = await fetch('https://api.groq.com/openai/v1/audio/transcriptions', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${this.apiKey}`,
        },
        body: formData,
      });

      if (!response.ok) {
        const error = await response.text();
        throw new Error(`Groq API error: ${response.status} - ${error}`);
      }

      const data = await response.json() as { text: string };
      const text = data.text?.trim();

      if (text) {
        const result: STTResult = {
          text,
          confidence: 1.0, // Groq doesn't provide confidence
          isFinal: true,
        };

        this.logger?.debug({ text }, 'Groq transcription received');
        this.emit('transcript', result);
        this.emit('final_transcript', result);
      }
    } catch (error: any) {
      this.logger?.error({
        error: error?.message || String(error),
        stack: error?.stack,
        name: error?.name
      }, 'Groq transcription error');
      this.emit('error', error);
    }
  }

  /**
   * Create a WAV buffer from raw PCM data.
   * Assumes 16-bit PCM at 16kHz mono.
   */
  private createWavBuffer(pcmData: Buffer): Buffer {
    const sampleRate = 16000;
    const numChannels = 1;
    const bitsPerSample = 16;
    const byteRate = sampleRate * numChannels * (bitsPerSample / 8);
    const blockAlign = numChannels * (bitsPerSample / 8);
    const dataSize = pcmData.length;
    const headerSize = 44;

    const buffer = Buffer.alloc(headerSize + dataSize);

    // RIFF header
    buffer.write('RIFF', 0);
    buffer.writeUInt32LE(36 + dataSize, 4);
    buffer.write('WAVE', 8);

    // fmt chunk
    buffer.write('fmt ', 12);
    buffer.writeUInt32LE(16, 16); // Subchunk1Size
    buffer.writeUInt16LE(1, 20); // AudioFormat (PCM)
    buffer.writeUInt16LE(numChannels, 22);
    buffer.writeUInt32LE(sampleRate, 24);
    buffer.writeUInt32LE(byteRate, 28);
    buffer.writeUInt16LE(blockAlign, 32);
    buffer.writeUInt16LE(bitsPerSample, 34);

    // data chunk
    buffer.write('data', 36);
    buffer.writeUInt32LE(dataSize, 40);
    pcmData.copy(buffer, 44);

    return buffer;
  }

  /**
   * Signal end of audio stream and request final results.
   */
  finalize(): void {
    if (this.silenceTimeout) {
      clearTimeout(this.silenceTimeout);
    }
    this.transcribeBuffer();
  }

  /**
   * Disconnect from the STT provider.
   */
  disconnect(): void {
    if (this.silenceTimeout) {
      clearTimeout(this.silenceTimeout);
    }
    this.isConnected = false;
    this.audioBuffer = [];
    this.logger?.info('Groq STT disconnected');
    this.emit('disconnected');
  }

  /**
   * Check if connected.
   */
  get connected(): boolean {
    return this.isConnected;
  }
}

/**
 * Create a Groq STT instance.
 */
export function createGroqSTT(options: GroqSTTOptions): GroqSTT {
  return new GroqSTT(options);
}
