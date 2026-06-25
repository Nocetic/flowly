/**
 * TTS (Text-to-Speech) provider factory.
 */

import { Logger } from 'pino';
import { OpenAITTS, createOpenAITTS, OPENAI_VOICES, OPENAI_TTS_MODELS } from './openai.js';
import { DeepgramTTS, createDeepgramTTS, DEEPGRAM_VOICES } from './deepgram.js';
import { ElevenLabsTTS, createElevenLabsTTS, ELEVENLABS_VOICES, ELEVENLABS_MODELS, type ElevenLabsVoiceSettings } from './elevenlabs.js';

export type TTSProvider = OpenAITTS | DeepgramTTS | ElevenLabsTTS;

export interface TTSOptions {
  provider: 'openai' | 'deepgram' | 'elevenlabs';
  openaiApiKey?: string;
  deepgramApiKey?: string;
  elevenlabsApiKey?: string;
  voice?: string;
  model?: string;
  speed?: number;
  // ElevenLabs-specific settings
  elevenlabsVoiceSettings?: ElevenLabsVoiceSettings;
  logger?: Logger;
}

/**
 * Create a TTS provider based on configuration.
 */
export function createTTS(options: TTSOptions): TTSProvider {
  switch (options.provider) {
    case 'openai':
      if (!options.openaiApiKey) {
        throw new Error('OpenAI API key is required for TTS');
      }
      return createOpenAITTS({
        apiKey: options.openaiApiKey,
        voice: options.voice,
        model: options.model,
        speed: options.speed,
        logger: options.logger,
      });

    case 'deepgram':
      if (!options.deepgramApiKey) {
        throw new Error('Deepgram API key is required for TTS');
      }
      return createDeepgramTTS({
        apiKey: options.deepgramApiKey,
        voice: options.voice,
        logger: options.logger,
      });

    case 'elevenlabs':
      if (!options.elevenlabsApiKey) {
        throw new Error('ElevenLabs API key is required for TTS');
      }
      return createElevenLabsTTS({
        apiKey: options.elevenlabsApiKey,
        voiceId: options.voice,
        modelId: options.model,
        voiceSettings: options.elevenlabsVoiceSettings,
        logger: options.logger,
      });

    default:
      throw new Error(`Unknown TTS provider: ${options.provider}`);
  }
}

export {
  OpenAITTS,
  DeepgramTTS,
  ElevenLabsTTS,
  OPENAI_VOICES,
  OPENAI_TTS_MODELS,
  DEEPGRAM_VOICES,
  ELEVENLABS_VOICES,
  ELEVENLABS_MODELS,
};
export type { ElevenLabsVoiceSettings };
