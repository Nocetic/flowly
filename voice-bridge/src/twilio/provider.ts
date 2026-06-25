/**
 * Twilio provider for making and managing voice calls.
 */

import Twilio from 'twilio';
import type { Twilio as TwilioClient } from 'twilio';
import { createHmac, timingSafeEqual } from 'crypto';
import { Logger } from 'pino';
import type { CallDirection, CallRecord, CallState } from '../types.js';
import { TwiMLBuilder, gatherSpeechTwiML } from './twiml.js';

// Type aliases for Twilio SDK
type CallInstance = Awaited<ReturnType<TwilioClient['calls']['create']>>;
type CallCreateOptions = Parameters<TwilioClient['calls']['create']>[0];

export interface TwilioProviderOptions {
  accountSid: string;
  authToken: string;
  phoneNumber: string;
  webhookBaseUrl?: string;
  voice?: string;
  language?: string;
  timeoutSeconds?: number;
  recording?: boolean;
  logger?: Logger;
}

export class TwilioProvider {
  private client: Twilio.Twilio;
  private logger?: Logger;
  private options: TwilioProviderOptions;

  constructor(options: TwilioProviderOptions) {
    this.options = options;
    this.logger = options.logger;
    this.client = Twilio(options.accountSid, options.authToken);
  }

  /**
   * Make an outbound call with a simple message.
   */
  async makeCall(options: {
    to: string;
    message?: string;
    twiml?: string;
    twimlUrl?: string;
    statusCallback?: string;
    timeout?: number;
    record?: boolean;
    metadata?: Record<string, unknown>;
  }): Promise<CallRecord> {
    const {
      to,
      message,
      twiml,
      twimlUrl,
      statusCallback,
      timeout,
      record,
      metadata,
    } = options;

    // Build TwiML if message provided
    let callTwiml = twiml;
    if (!callTwiml && !twimlUrl && message) {
      callTwiml = new TwiMLBuilder()
        .say(message, {
          voice: this.options.voice,
          language: this.options.language,
        })
        .hangup()
        .build();
    }

    // Prepare call params
    const callParams: CallCreateOptions = {
      to: this.formatPhoneNumber(to),
      from: this.options.phoneNumber,
      timeout: timeout || this.options.timeoutSeconds || 30,
    };

    if (twimlUrl) {
      callParams.url = twimlUrl;
    } else if (callTwiml) {
      callParams.twiml = callTwiml;
    } else {
      throw new Error('Must provide message, twiml, or twimlUrl');
    }

    if (statusCallback) {
      callParams.statusCallback = statusCallback;
      callParams.statusCallbackEvent = ['initiated', 'ringing', 'answered', 'completed'];
      callParams.statusCallbackMethod = 'POST';
    }

    const shouldRecord = record ?? this.options.recording ?? false;
    if (shouldRecord) {
      callParams.record = true;
    }

    this.logger?.info({ to, hasMessage: !!message }, 'Making outbound call');

    // Make the call
    const call = await this.client.calls.create(callParams);

    // Create call record
    const callRecord: CallRecord = {
      callSid: call.sid,
      accountSid: call.accountSid,
      direction: 'outbound',
      from: call.from,
      to: call.to,
      state: this.parseStatus(call.status),
      createdAt: new Date(),
      transcript: [],
      metadata: metadata || {},
    };

    this.logger?.info({ callSid: call.sid, to }, 'Call initiated');

    return callRecord;
  }

  /**
   * Make an outbound call for conversation (with speech gathering).
   */
  async makeConversationCall(options: {
    to: string;
    greeting: string;
    gatherAction: string;
    statusCallback?: string;
    hints?: string;
    metadata?: Record<string, unknown>;
  }): Promise<CallRecord> {
    const { to, greeting, gatherAction, statusCallback, hints, metadata } = options;

    const twiml = gatherSpeechTwiML(greeting, gatherAction, {
      voice: this.options.voice,
      language: this.options.language,
      hints,
    });

    return this.makeCall({
      to,
      twiml,
      statusCallback,
      metadata,
    });
  }

  /**
   * Make a call with Media Streams for real-time audio.
   */
  async makeMediaStreamCall(options: {
    to: string;
    websocketUrl: string;
    statusCallback?: string;
    metadata?: Record<string, unknown>;
  }): Promise<CallRecord> {
    const { to, websocketUrl, statusCallback, metadata } = options;

    const twiml = new TwiMLBuilder()
      .connect()
      .stream({ url: websocketUrl, track: 'inbound_track' })
      .endConnect()
      .build();

    return this.makeCall({
      to,
      twiml,
      statusCallback,
      metadata,
    });
  }

  /**
   * Update an in-progress call.
   */
  async updateCall(options: {
    callSid: string;
    twiml?: string;
    twimlUrl?: string;
    status?: 'completed' | 'canceled';
  }): Promise<void> {
    const { callSid, twiml, twimlUrl, status } = options;

    const updateParams: Record<string, any> = {};

    if (twiml) {
      updateParams.twiml = twiml;
    } else if (twimlUrl) {
      updateParams.url = twimlUrl;
    }

    if (status) {
      updateParams.status = status;
    }

    this.logger?.debug({ callSid, hasUpdate: Object.keys(updateParams).length > 0 }, 'Updating call');

    await this.client.calls(callSid).update(updateParams);
  }

  /**
   * End a call.
   */
  async endCall(callSid: string): Promise<void> {
    await this.updateCall({ callSid, status: 'completed' });
    this.logger?.info({ callSid }, 'Call ended');
  }

  /**
   * Get call details.
   */
  async getCall(callSid: string): Promise<CallInstance> {
    return this.client.calls(callSid).fetch();
  }

  /**
   * List calls with optional filters.
   */
  async listCalls(options: {
    to?: string;
    from?: string;
    status?: string;
    startTimeAfter?: Date;
    limit?: number;
  } = {}): Promise<CallInstance[]> {
    const { to, from, status, startTimeAfter, limit = 50 } = options;

    const listParams: any = { limit };

    if (to) listParams.to = to;
    if (from) listParams.from = from;
    if (status) listParams.status = status;
    if (startTimeAfter) listParams.startTimeAfter = startTimeAfter;

    return this.client.calls.list(listParams);
  }

  /**
   * Verify Twilio webhook signature.
   * CRITICAL for security - always verify webhooks!
   */
  verifySignature(
    signature: string,
    url: string,
    params: Record<string, string>
  ): boolean {
    // Sort params and create data string
    const sortedParams = Object.keys(params)
      .sort()
      .map((key) => `${key}${params[key]}`)
      .join('');

    const dataString = url + sortedParams;

    // Compute HMAC-SHA1
    const expected = createHmac('sha1', this.options.authToken)
      .update(dataString, 'utf-8')
      .digest('base64');

    // Timing-safe comparison
    try {
      return timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
    } catch {
      return false;
    }
  }

  /**
   * Generate a webhook URL.
   */
  generateWebhookUrl(path: string): string {
    if (!this.options.webhookBaseUrl) {
      throw new Error('webhookBaseUrl not configured');
    }
    return `${this.options.webhookBaseUrl.replace(/\/$/, '')}${path}`;
  }

  /**
   * Parse Twilio status to CallState.
   */
  parseStatus(status: string): CallState {
    const statusMap: Record<string, CallState> = {
      queued: 'queued',
      initiated: 'initiated',
      ringing: 'ringing',
      'in-progress': 'in-progress',
      completed: 'completed',
      busy: 'busy',
      failed: 'failed',
      'no-answer': 'no-answer',
      canceled: 'canceled',
    };
    return statusMap[status.toLowerCase()] || 'initiated';
  }

  /**
   * Format phone number to E.164 format.
   */
  formatPhoneNumber(number: string): string {
    // Remove all non-digit characters except +
    let cleaned = number.replace(/[^\d+]/g, '');

    // Ensure it starts with +
    if (!cleaned.startsWith('+')) {
      // Assume US if no country code
      if (cleaned.length === 10) {
        cleaned = '+1' + cleaned;
      } else if (cleaned.length === 11 && cleaned.startsWith('1')) {
        cleaned = '+' + cleaned;
      } else {
        cleaned = '+' + cleaned;
      }
    }

    return cleaned;
  }

  /**
   * Get the Twilio client for advanced usage.
   */
  get twilioClient(): Twilio.Twilio {
    return this.client;
  }

  /**
   * Get the configured phone number.
   */
  get fromNumber(): string {
    return this.options.phoneNumber;
  }
}

/**
 * Create a Twilio provider instance.
 */
export function createTwilioProvider(options: TwilioProviderOptions): TwilioProvider {
  return new TwilioProvider(options);
}
