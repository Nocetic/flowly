/**
 * ElevenLabs TTS (Text-to-Speech) provider.
 *
 * Uses ElevenLabs' TTS API for high-quality voice synthesis.
 */

import { Logger } from 'pino';
import { convertToTwilioAudio } from '../audio.js';

export interface ElevenLabsVoiceSettings {
  stability?: number;        // 0-1, default: 0.5
  similarityBoost?: number;  // 0-1, default: 0.75
  style?: number;            // 0-1, default: 0.0
  useSpeakerBoost?: boolean; // default: true
  speed?: number;            // 0.5-2.0, default: 1.0
}

export interface ElevenLabsTTSOptions {
  apiKey: string;
  voiceId?: string;
  modelId?: string;
  voiceSettings?: ElevenLabsVoiceSettings;
  logger?: Logger;
}

export class ElevenLabsTTS {
  private apiKey: string;
  private voiceId: string;
  private modelId: string;
  private voiceSettings: Required<ElevenLabsVoiceSettings>;
  private logger?: Logger;
  private baseUrl = 'https://api.elevenlabs.io';

  constructor(options: ElevenLabsTTSOptions) {
    this.apiKey = options.apiKey;
    this.voiceId = options.voiceId || 'pMsXgVXv3BLzUgSXRplE'; // Default: a female voice
    this.modelId = options.modelId || 'eleven_multilingual_v2';
    this.logger = options.logger;

    // Merge voice settings with defaults
    this.voiceSettings = {
      stability: options.voiceSettings?.stability ?? 0.5,
      similarityBoost: options.voiceSettings?.similarityBoost ?? 0.75,
      style: options.voiceSettings?.style ?? 0.0,
      useSpeakerBoost: options.voiceSettings?.useSpeakerBoost ?? true,
      speed: options.voiceSettings?.speed ?? 1.0,
    };
  }

  /**
   * Generate speech audio from text.
   *
   * @param text Text to convert to speech
   * @returns PCM audio buffer (24kHz, 16-bit, mono)
   */
  async synthesize(text: string): Promise<Buffer> {
    this.logger?.debug({ text: text.substring(0, 50) }, 'Synthesizing speech with ElevenLabs');

    // Use PCM format at 24kHz for consistency with other providers
    const outputFormat = 'pcm_24000';
    const url = `${this.baseUrl}/v1/text-to-speech/${this.voiceId}?output_format=${outputFormat}`;

    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'xi-api-key': this.apiKey,
        'Content-Type': 'application/json',
        'Accept': 'audio/pcm',
      },
      body: JSON.stringify({
        text,
        model_id: this.modelId,
        voice_settings: {
          stability: this.voiceSettings.stability,
          similarity_boost: this.voiceSettings.similarityBoost,
          style: this.voiceSettings.style,
          use_speaker_boost: this.voiceSettings.useSpeakerBoost,
          speed: this.voiceSettings.speed,
        },
      }),
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`ElevenLabs TTS error: ${response.status} - ${errorText}`);
    }

    const arrayBuffer = await response.arrayBuffer();
    const pcmBuffer = Buffer.from(arrayBuffer);

    // Add 200ms of silence at the end to prevent audio cutoff artifacts
    const silencePadding = Buffer.alloc(24000 * 2 * 0.2); // 200ms at 24kHz 16-bit
    const paddedBuffer = Buffer.concat([pcmBuffer, silencePadding]);

    this.logger?.debug({ bytes: paddedBuffer.length }, 'Speech synthesized with ElevenLabs');

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

    // ElevenLabs outputs 24kHz PCM
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
 * Create an ElevenLabs TTS instance.
 */
export function createElevenLabsTTS(options: ElevenLabsTTSOptions): ElevenLabsTTS {
  return new ElevenLabsTTS(options);
}

// Popular ElevenLabs voices
export const ELEVENLABS_VOICES = {
  // Premade voices
  'rachel': '21m00Tcm4TlvDq8ikWAM',     // Female, American, calm
  'domi': 'AZnzlk1XvdvUeBnXmlld',       // Female, American, strong
  'bella': 'EXAVITQu4vr4xnSDxMaL',      // Female, American, soft
  'antoni': 'ErXwobaYiN019PkySvjV',     // Male, American, well-rounded
  'elli': 'MF3mGyEYCl7XYWbV9V6O',       // Female, American, young
  'josh': 'TxGEqnHWrfWFTfGW9XjX',       // Male, American, deep
  'arnold': 'VR6AewLTigWG4xSOukaG',     // Male, American, crisp
  'adam': 'pNInz6obpgDQGcFmaJgB',       // Male, American, deep
  'sam': 'yoZ06aMxZJJ28mfd3POQ',        // Male, American, raspy
  // Default voice
  'default': 'pMsXgVXv3BLzUgSXRplE',    // Female
} as const;

// Available models
export const ELEVENLABS_MODELS = [
  'eleven_multilingual_v2',   // Multilingual, high quality
  'eleven_turbo_v2_5',        // Fast, English-focused
  'eleven_monolingual_v1',    // Original English model
] as const;

export type ElevenLabsVoice = keyof typeof ELEVENLABS_VOICES;
export type ElevenLabsModel = typeof ELEVENLABS_MODELS[number];
