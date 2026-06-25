/**
 * Type definitions for voice bridge.
 */

export type CallDirection = 'inbound' | 'outbound';

export type CallState =
  | 'initiated'
  | 'queued'
  | 'ringing'
  | 'in-progress'
  | 'completed'
  | 'busy'
  | 'failed'
  | 'no-answer'
  | 'canceled';

// Conversation state for agentic behavior
export type ConversationState =
  | 'idle'        // Not in active conversation
  | 'speaking'    // System is speaking
  | 'listening'   // Waiting for user input
  | 'processing'; // Processing user input, generating response

export interface CallRecord {
  callSid: string;
  accountSid: string;
  direction: CallDirection;
  from: string;
  to: string;
  state: CallState;

  // Timestamps
  createdAt: Date;
  answeredAt?: Date;
  endedAt?: Date;

  // Duration
  durationSeconds?: number;

  // Recording
  recordingUrl?: string;

  // Conversation
  transcript: TranscriptEntry[];

  // Custom metadata
  metadata: Record<string, unknown>;

  // Media stream
  streamSid?: string;

  // Conversation state for agentic behavior
  conversationState?: ConversationState;
}

export interface TranscriptEntry {
  role: 'user' | 'assistant';
  text: string;
  timestamp: Date;
  confidence?: number;
}

export type CallEventType =
  | 'state_changed'
  | 'transcription'
  | 'speech_started'
  | 'speech_ended'
  | 'dtmf'
  | 'error'
  | 'stream_connected'
  | 'stream_disconnected';

export interface CallEvent {
  callSid: string;
  eventType: CallEventType;
  timestamp: Date;
  data: Record<string, unknown>;
}

// Twilio Media Stream message types
export interface TwilioMediaMessage {
  event: 'connected' | 'start' | 'media' | 'stop' | 'mark';
  sequenceNumber?: string;
  streamSid?: string;
  start?: {
    streamSid: string;
    accountSid: string;
    callSid: string;
    tracks: string[];
    customParameters: Record<string, string>;
    mediaFormat: {
      encoding: string;
      sampleRate: number;
      channels: number;
    };
  };
  media?: {
    track: string;
    chunk: string;
    timestamp: string;
    payload: string; // Base64 encoded audio
  };
  stop?: {
    accountSid: string;
    callSid: string;
  };
  mark?: {
    name: string;
  };
}

// Outbound message to Twilio Media Stream
export interface TwilioOutboundMessage {
  event: 'media' | 'mark' | 'clear';
  streamSid: string;
  media?: {
    payload: string; // Base64 encoded mu-law audio
  };
  mark?: {
    name: string;
  };
}

// STT result
export interface STTResult {
  text: string;
  confidence: number;
  isFinal: boolean;
  words?: Array<{
    word: string;
    start: number;
    end: number;
    confidence: number;
  }>;
}

// TTS request
export interface TTSRequest {
  text: string;
  voice?: string;
  speed?: number;
}

// Agent message
export interface AgentMessage {
  type: 'call_request' | 'speak' | 'end_call' | 'get_status';
  callSid?: string;
  to?: string;
  message?: string;
  metadata?: Record<string, unknown>;
}

// Agent response
export interface AgentResponse {
  type: 'call_initiated' | 'transcription' | 'call_ended' | 'status' | 'error';
  callSid?: string;
  text?: string;
  state?: CallState;
  transcript?: TranscriptEntry[];
  error?: string;
}

// Flowly gateway message format
export interface FlowlyInboundMessage {
  channel: string;
  sender_id: string;
  chat_id: string;
  content: string;
  media?: string[];
}

export interface FlowlyOutboundMessage {
  channel: string;
  chat_id: string;
  content: string;
  media_paths?: string[];
}
