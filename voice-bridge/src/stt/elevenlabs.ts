/**
 * ElevenLabs STT (Speech-to-Text) provider.
 *
 * Uses ElevenLabs' Realtime Speech-to-Text WebSocket API (Scribe v2).
 * Provides streaming transcription with ~150ms latency.
 */

import { EventEmitter } from 'events';
import { Logger } from 'pino';
import WebSocket from 'ws';
import type { STTResult } from '../types.js';

export interface ElevenLabsSTTOptions {
  apiKey: string;
  language?: string;
  model?: string;
  logger?: Logger;
}

// ElevenLabs message types
interface SessionStartedMessage {
  message_type: 'session_started';
  session_id: string;
  model_id: string;
  sample_rate: number;
  audio_format: string;
  language_code: string;
}

interface PartialTranscriptMessage {
  message_type: 'partial_transcript';
  text: string;
}

interface CommittedTranscriptMessage {
  message_type: 'committed_transcript';
  text: string;
}

interface CommittedTranscriptWithTimestampsMessage {
  message_type: 'committed_transcript_with_timestamps';
  text: string;
  language: string;
  words: Array<{
    text: string;
    start: number;
    end: number;
  }>;
}

interface ErrorMessage {
  message_type: 'auth_error' | 'quota_exceeded' | 'rate_limited' | 'input_error' | 'error';
  error?: string;
  message?: string;
}

type ElevenLabsMessage =
  | SessionStartedMessage
  | PartialTranscriptMessage
  | CommittedTranscriptMessage
  | CommittedTranscriptWithTimestampsMessage
  | ErrorMessage;

export class ElevenLabsSTT extends EventEmitter {
  private apiKey: string;
  private language: string;
  private model: string;
  private logger?: Logger;
  private ws: WebSocket | null = null;
  private isConnected = false;
  private reconnectAttempts = 0;
  private readonly MAX_RECONNECT_ATTEMPTS = 3;
  private pendingAudio: Buffer[] = [];
  private sessionId: string | null = null;

  constructor(options: ElevenLabsSTTOptions) {
    super();
    this.apiKey = options.apiKey;
    // ElevenLabs uses ISO 639-1 codes (e.g., 'en', 'tr')
    const lang = options.language || 'en';
    this.language = lang.split('-')[0];
    this.model = options.model || 'scribe_v2_realtime';
    this.logger = options.logger;
  }

  /**
   * Connect to the ElevenLabs STT WebSocket.
   */
  async connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      try {
        // Build WebSocket URL with parameters
        // Audio format: pcm_16000 (16kHz PCM, which matches Twilio's format after conversion)
        const params = new URLSearchParams({
          model_id: this.model,
          language_code: this.language,
          audio_format: 'pcm_16000',
        });

        const url = `wss://api.elevenlabs.io/v1/speech-to-text/realtime?${params.toString()}`;

        this.ws = new WebSocket(url, {
          headers: {
            'xi-api-key': this.apiKey,
          },
        });

        this.ws.on('open', () => {
          this.isConnected = true;
          this.reconnectAttempts = 0;
          this.logger?.info('ElevenLabs STT WebSocket connected');

          // Send any pending audio
          for (const audio of this.pendingAudio) {
            this.sendAudioChunk(audio);
          }
          this.pendingAudio = [];

          this.emit('connected');
          resolve();
        });

        this.ws.on('message', (data: Buffer) => {
          try {
            const message: ElevenLabsMessage = JSON.parse(data.toString());
            this.handleMessage(message);
          } catch (error) {
            this.logger?.error({ error }, 'Failed to parse ElevenLabs STT message');
          }
        });

        this.ws.on('close', (code, reason) => {
          this.isConnected = false;
          this.sessionId = null;
          this.logger?.info({ code, reason: reason.toString() }, 'ElevenLabs STT WebSocket closed');

          // Try to reconnect if not intentional close
          if (code !== 1000 && this.reconnectAttempts < this.MAX_RECONNECT_ATTEMPTS) {
            this.reconnectAttempts++;
            this.logger?.info({ attempt: this.reconnectAttempts }, 'Attempting to reconnect...');
            setTimeout(() => this.connect(), 1000 * this.reconnectAttempts);
          }

          this.emit('disconnected');
        });

        this.ws.on('error', (error) => {
          this.logger?.error({ error }, 'ElevenLabs STT WebSocket error');
          this.emit('error', error);
          if (!this.isConnected) {
            reject(error);
          }
        });
      } catch (error) {
        this.logger?.error({ error }, 'Failed to connect to ElevenLabs STT');
        reject(error);
      }
    });
  }

  /**
   * Handle incoming WebSocket messages.
   */
  private handleMessage(message: ElevenLabsMessage): void {
    switch (message.message_type) {
      case 'session_started':
        this.sessionId = message.session_id;
        this.logger?.info({ sessionId: message.session_id, model: message.model_id }, 'ElevenLabs STT session started');
        break;

      case 'partial_transcript':
        if (message.text) {
          const result: STTResult = {
            text: message.text,
            confidence: 1.0,
            isFinal: false,
          };
          this.logger?.debug({ text: message.text }, 'ElevenLabs partial transcript');
          this.emit('transcript', result);
        }
        break;

      case 'committed_transcript':
        if (message.text) {
          const result: STTResult = {
            text: message.text,
            confidence: 1.0,
            isFinal: true,
          };
          this.logger?.info({ text: message.text }, 'ElevenLabs final transcript');
          this.emit('transcript', result);
          this.emit('final_transcript', result);
        }
        break;

      case 'committed_transcript_with_timestamps':
        if (message.text) {
          const result: STTResult = {
            text: message.text,
            confidence: 1.0,
            isFinal: true,
          };
          this.logger?.info({ text: message.text, language: message.language }, 'ElevenLabs final transcript with timestamps');
          this.emit('transcript', result);
          this.emit('final_transcript', result);
        }
        break;

      case 'auth_error':
      case 'quota_exceeded':
      case 'rate_limited':
      case 'input_error':
      case 'error':
        const errorMsg = message.error || message.message || `ElevenLabs STT error: ${message.message_type}`;
        this.logger?.error({ type: message.message_type, error: errorMsg }, 'ElevenLabs STT error');
        this.emit('error', new Error(errorMsg));
        break;

      default:
        this.logger?.debug({ message }, 'Unknown ElevenLabs message type');
    }
  }

  /**
   * Send an audio chunk in the correct format.
   */
  private sendAudioChunk(audioData: Buffer, commit: boolean = false): void {
    if (!this.ws || !this.isConnected) return;

    try {
      // ElevenLabs expects base64-encoded audio in a JSON message
      const message = {
        message_type: 'input_audio_chunk',
        audio_base_64: audioData.toString('base64'),
        commit: commit,
      };

      this.ws.send(JSON.stringify(message));
    } catch (error) {
      this.logger?.error({ error }, 'Failed to send audio chunk to ElevenLabs STT');
    }
  }

  /**
   * Send audio data for transcription.
   * Audio should be 16kHz 16-bit PCM.
   */
  send(audioData: Buffer): void {
    if (!this.isConnected || !this.ws) {
      // Buffer audio if not yet connected
      this.pendingAudio.push(audioData);
      return;
    }

    this.sendAudioChunk(audioData);
  }

  /**
   * Signal end of audio stream and request final results.
   */
  finalize(): void {
    if (this.ws && this.isConnected) {
      try {
        // Send a commit message to finalize transcription
        const message = {
          message_type: 'input_audio_chunk',
          audio_base_64: '',
          commit: true,
        };
        this.ws.send(JSON.stringify(message));
      } catch (error) {
        this.logger?.error({ error }, 'Failed to finalize ElevenLabs STT');
      }
    }
  }

  /**
   * Disconnect from the STT provider.
   */
  disconnect(): void {
    if (this.ws) {
      this.ws.close(1000, 'Client disconnect');
      this.ws = null;
    }
    this.isConnected = false;
    this.sessionId = null;
    this.pendingAudio = [];
    this.logger?.info('ElevenLabs STT disconnected');
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
 * Create an ElevenLabs STT instance.
 */
export function createElevenLabsSTT(options: ElevenLabsSTTOptions): ElevenLabsSTT {
  return new ElevenLabsSTT(options);
}
