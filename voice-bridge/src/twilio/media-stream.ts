/**
 * Twilio Media Stream handler for real-time bidirectional audio.
 *
 * Handles WebSocket connection from Twilio for streaming audio.
 * Supports both 'ws' library and Fastify WebSocket.
 */

import { EventEmitter } from 'events';
import { Logger } from 'pino';
import type { TwilioMediaMessage, TwilioOutboundMessage } from '../types.js';
import { convertFromTwilioAudio, TWILIO_BYTES_PER_FRAME } from '../audio.js';

// Generic WebSocket interface that works with both ws and Fastify
interface GenericWebSocket {
  send(data: string | Buffer): void;
  close?(): void;
  terminate?(): void;
  readyState?: number;
  on?(event: string, handler: (...args: any[]) => void): void;
  addEventListener?(event: string, handler: (event: any) => void): void;
}

export interface MediaStreamOptions {
  logger?: Logger;
  onAudio?: (audio: Buffer) => void;
  onConnected?: (callSid: string, streamSid: string) => void;
  onDisconnected?: (callSid: string) => void;
}

export class MediaStreamHandler extends EventEmitter {
  private ws: GenericWebSocket;
  private logger?: Logger;
  private streamSid: string | null = null;
  private callSid: string | null = null;
  private audioBuffer: Buffer[] = [];
  private markCounter = 0;
  private pendingMarks: Map<string, () => void> = new Map();
  private isSpeaking = false;

  constructor(ws: GenericWebSocket, options: MediaStreamOptions = {}) {
    super();
    this.ws = ws;
    this.logger = options.logger;

    this.setupHandlers();

    if (options.onAudio) {
      this.on('audio', options.onAudio);
    }
    if (options.onConnected) {
      this.on('connected', options.onConnected);
    }
    if (options.onDisconnected) {
      this.on('disconnected', options.onDisconnected);
    }
  }

  /**
   * Set up WebSocket message handlers.
   * Supports both 'ws' library (on) and browser/Fastify WebSocket (addEventListener).
   */
  private setupHandlers(): void {
    const messageHandler = (data: any) => {
      try {
        // Handle different data formats
        let strData: string;
        if (typeof data === 'string') {
          strData = data;
        } else if (data instanceof Buffer) {
          strData = data.toString();
        } else if (data?.data) {
          // Browser-style MessageEvent
          strData = typeof data.data === 'string' ? data.data : data.data.toString();
        } else {
          strData = String(data);
        }

        const message: TwilioMediaMessage = JSON.parse(strData);
        this.handleMessage(message);
      } catch (error) {
        this.logger?.error({ error }, 'Failed to parse media stream message');
      }
    };

    const closeHandler = () => {
      this.logger?.info({ callSid: this.callSid }, 'Media stream closed');
      this.emit('disconnected', this.callSid);
    };

    const errorHandler = (error: any) => {
      this.logger?.error({ error }, 'Media stream WebSocket error');
      this.emit('error', error);
    };

    // Try ws-style .on() first, then browser-style addEventListener
    if (typeof this.ws.on === 'function') {
      this.ws.on('message', messageHandler);
      this.ws.on('close', closeHandler);
      this.ws.on('error', errorHandler);
    } else if (typeof this.ws.addEventListener === 'function') {
      this.ws.addEventListener('message', messageHandler);
      this.ws.addEventListener('close', closeHandler);
      this.ws.addEventListener('error', errorHandler);
    } else {
      this.logger?.error('WebSocket does not support on() or addEventListener()');
    }
  }

  /**
   * Handle incoming Twilio media stream message.
   */
  private handleMessage(message: TwilioMediaMessage): void {
    switch (message.event) {
      case 'connected':
        this.logger?.debug('Media stream connected (waiting for start)');
        break;

      case 'start':
        this.handleStart(message);
        break;

      case 'media':
        this.handleMedia(message);
        break;

      case 'stop':
        this.handleStop(message);
        break;

      case 'mark':
        this.handleMark(message);
        break;

      default:
        this.logger?.debug({ event: message.event }, 'Unknown media stream event');
    }
  }

  /**
   * Handle stream start message.
   */
  private handleStart(message: TwilioMediaMessage): void {
    if (!message.start) return;

    this.streamSid = message.start.streamSid;
    this.callSid = message.start.callSid;

    this.logger?.info(
      {
        callSid: this.callSid,
        streamSid: this.streamSid,
        tracks: message.start.tracks,
        mediaFormat: message.start.mediaFormat,
      },
      'Media stream started'
    );

    this.emit('connected', this.callSid, this.streamSid);
  }

  /**
   * Handle incoming audio media.
   */
  private handleMedia(message: TwilioMediaMessage): void {
    if (!message.media) return;

    // Decode base64 mu-law audio
    const audioData = Buffer.from(message.media.payload, 'base64');
    this.audioBuffer.push(audioData);

    // Log every 50 frames (~1 second of audio)
    if (this.audioBuffer.length % 50 === 0) {
      this.logger?.debug({ frames: this.audioBuffer.length }, 'Receiving audio frames');
    }

    // Emit audio in batches (e.g., every 200ms = 10 frames)
    if (this.audioBuffer.length >= 10) {
      this.flushAudioBuffer();
    }
  }

  /**
   * Flush accumulated audio buffer.
   */
  private flushAudioBuffer(): void {
    if (this.audioBuffer.length === 0) return;

    // Convert mu-law to PCM at 16kHz for STT
    const pcmAudio = convertFromTwilioAudio(this.audioBuffer, 16000);
    this.logger?.debug({ inputFrames: this.audioBuffer.length, outputBytes: pcmAudio.length }, 'Flushing audio to STT');
    this.audioBuffer = [];

    this.emit('audio', pcmAudio);
  }

  /**
   * Handle stream stop message.
   */
  private handleStop(message: TwilioMediaMessage): void {
    this.logger?.info({ callSid: this.callSid }, 'Media stream stopped');

    // Flush any remaining audio
    this.flushAudioBuffer();

    this.emit('disconnected', this.callSid);
  }

  /**
   * Handle mark message (audio playback completed).
   */
  private handleMark(message: TwilioMediaMessage): void {
    if (!message.mark) return;

    const markName = message.mark.name;
    this.logger?.debug({ markName }, 'Mark received');

    // Resolve pending mark promise
    const resolver = this.pendingMarks.get(markName);
    if (resolver) {
      resolver();
      this.pendingMarks.delete(markName);
    }

    // Check if this was the last mark (speaking finished)
    if (this.pendingMarks.size === 0 && this.isSpeaking) {
      this.isSpeaking = false;
      this.emit('speaking_finished');
    }

    this.emit('mark', markName);
  }

  /**
   * Send audio to Twilio (for playback to caller).
   * Audio should be mu-law encoded, base64 string.
   */
  sendAudio(mulawBase64: string): void {
    if (!this.streamSid) {
      this.logger?.warn('Cannot send audio: stream not connected');
      return;
    }

    const message: TwilioOutboundMessage = {
      event: 'media',
      streamSid: this.streamSid,
      media: {
        payload: mulawBase64,
      },
    };

    this.ws.send(JSON.stringify(message));
  }

  /**
   * Send audio frames (array of mu-law buffers).
   * Returns a promise that resolves when playback is complete.
   */
  async sendAudioFrames(frames: Buffer[]): Promise<void> {
    if (!this.streamSid || frames.length === 0) return;

    this.isSpeaking = true;
    const markName = `mark_${++this.markCounter}`;

    // Send all audio frames
    for (const frame of frames) {
      this.sendAudio(frame.toString('base64'));
    }

    // Send mark to know when playback is done
    const markMessage: TwilioOutboundMessage = {
      event: 'mark',
      streamSid: this.streamSid,
      mark: {
        name: markName,
      },
    };
    this.ws.send(JSON.stringify(markMessage));

    // Wait for mark acknowledgment
    return new Promise((resolve) => {
      this.pendingMarks.set(markName, resolve);
    });
  }

  /**
   * Clear the audio queue (for barge-in support).
   */
  clearAudio(): void {
    if (!this.streamSid) return;

    const message: TwilioOutboundMessage = {
      event: 'clear',
      streamSid: this.streamSid,
    };

    this.ws.send(JSON.stringify(message));
    this.pendingMarks.clear();
    this.isSpeaking = false;

    this.logger?.debug('Audio cleared');
  }

  /**
   * Check if currently speaking.
   */
  get speaking(): boolean {
    return this.isSpeaking;
  }

  /**
   * Get the stream SID.
   */
  get sid(): string | null {
    return this.streamSid;
  }

  /**
   * Get the call SID.
   */
  get call(): string | null {
    return this.callSid;
  }

  /**
   * Check if connected.
   */
  get connected(): boolean {
    // WebSocket.OPEN = 1
    return this.streamSid !== null && this.ws.readyState === 1;
  }

  /**
   * Close the WebSocket connection.
   */
  close(): void {
    this.flushAudioBuffer();
    if (typeof this.ws.close === 'function') {
      this.ws.close();
    } else if (typeof this.ws.terminate === 'function') {
      this.ws.terminate();
    }
  }
}

/**
 * Create a media stream handler.
 */
export function createMediaStreamHandler(
  ws: WebSocket,
  options?: MediaStreamOptions
): MediaStreamHandler {
  return new MediaStreamHandler(ws, options);
}
