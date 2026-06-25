/**
 * Deepgram STT (Speech-to-Text) provider.
 *
 * Uses Deepgram's real-time WebSocket API for streaming transcription.
 */

import { createClient, LiveTranscriptionEvents, LiveClient } from '@deepgram/sdk';
import { EventEmitter } from 'events';
import { Logger } from 'pino';
import type { STTResult } from '../types.js';

export interface DeepgramSTTOptions {
  apiKey: string;
  language?: string;
  model?: string;
  punctuate?: boolean;
  interimResults?: boolean;
  utteranceEndMs?: number;
  vadEvents?: boolean;
  logger?: Logger;
}

export class DeepgramSTT extends EventEmitter {
  private client: ReturnType<typeof createClient>;
  private connection: LiveClient | null = null;
  private logger?: Logger;
  private options: DeepgramSTTOptions;
  private isConnected = false;

  constructor(options: DeepgramSTTOptions) {
    super();
    this.options = options;
    this.logger = options.logger;
    this.client = createClient(options.apiKey);
  }

  /**
   * Connect to Deepgram's real-time transcription service.
   */
  async connect(): Promise<void> {
    if (this.isConnected) {
      return;
    }

    this.logger?.info('Connecting to Deepgram...');

    this.connection = this.client.listen.live({
      model: this.options.model || 'nova-2',
      language: this.options.language || 'en-US',
      punctuate: this.options.punctuate ?? true,
      interim_results: this.options.interimResults ?? true,
      utterance_end_ms: this.options.utteranceEndMs || 1000,
      vad_events: this.options.vadEvents ?? true,
      encoding: 'linear16',
      sample_rate: 16000,
      channels: 1,
    });

    return new Promise((resolve, reject) => {
      if (!this.connection) {
        reject(new Error('Connection not initialized'));
        return;
      }

      const timeout = setTimeout(() => {
        reject(new Error('Deepgram connection timeout'));
      }, 10000);

      this.connection.on(LiveTranscriptionEvents.Open, () => {
        clearTimeout(timeout);
        this.isConnected = true;
        this.logger?.info('Connected to Deepgram');
        this.emit('connected');
        resolve();
      });

      this.connection.on(LiveTranscriptionEvents.Transcript, (data) => {
        this.handleTranscript(data);
      });

      this.connection.on(LiveTranscriptionEvents.UtteranceEnd, () => {
        this.logger?.debug('Utterance end detected');
        this.emit('utterance_end');
      });

      this.connection.on(LiveTranscriptionEvents.SpeechStarted, () => {
        this.logger?.debug('Speech started');
        this.emit('speech_started');
      });

      this.connection.on(LiveTranscriptionEvents.Error, (error) => {
        this.logger?.error({ error }, 'Deepgram error');
        this.emit('error', error);
      });

      this.connection.on(LiveTranscriptionEvents.Close, () => {
        this.isConnected = false;
        this.logger?.info('Deepgram connection closed');
        this.emit('disconnected');
      });
    });
  }

  /**
   * Handle incoming transcript from Deepgram.
   */
  private handleTranscript(data: any): void {
    const transcript = data.channel?.alternatives?.[0];
    if (!transcript) return;

    const result: STTResult = {
      text: transcript.transcript || '',
      confidence: transcript.confidence || 0,
      isFinal: data.is_final || false,
      words: transcript.words?.map((w: any) => ({
        word: w.word,
        start: w.start,
        end: w.end,
        confidence: w.confidence,
      })),
    };

    if (result.text) {
      this.logger?.debug({ result }, 'Transcript received');
      this.emit('transcript', result);

      if (result.isFinal) {
        this.emit('final_transcript', result);
      } else {
        this.emit('interim_transcript', result);
      }
    }
  }

  /**
   * Send audio data to Deepgram for transcription.
   * Expects 16-bit PCM at 16kHz mono.
   */
  send(audioData: Buffer): void {
    if (!this.connection || !this.isConnected) {
      this.logger?.warn('Cannot send audio: not connected');
      return;
    }

    // Convert Buffer to ArrayBuffer for Deepgram SDK
    const arrayBuffer = audioData.buffer.slice(
      audioData.byteOffset,
      audioData.byteOffset + audioData.byteLength
    );
    this.connection.send(arrayBuffer);
  }

  /**
   * Signal end of audio stream and request final results.
   */
  finalize(): void {
    if (!this.connection || !this.isConnected) {
      return;
    }

    // Send empty buffer to signal end
    this.connection.send(new ArrayBuffer(0));
  }

  /**
   * Disconnect from Deepgram.
   */
  disconnect(): void {
    if (this.connection) {
      this.connection.finish();
      this.connection = null;
    }
    this.isConnected = false;
  }

  /**
   * Check if connected to Deepgram.
   */
  get connected(): boolean {
    return this.isConnected;
  }
}

/**
 * Create a Deepgram STT instance.
 */
export function createDeepgramSTT(options: DeepgramSTTOptions): DeepgramSTT {
  return new DeepgramSTT(options);
}
