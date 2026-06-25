/**
 * STT (Speech-to-Text) provider factory.
 */

import { EventEmitter } from 'events';
import { Logger } from 'pino';
import { DeepgramSTT, createDeepgramSTT } from './deepgram.js';
import { OpenAISTT, createOpenAISTT } from './openai.js';
import { GroqSTT, createGroqSTT } from './groq.js';
import { ElevenLabsSTT, createElevenLabsSTT } from './elevenlabs.js';
import type { STTResult } from '../types.js';

export type STTProvider = DeepgramSTT | OpenAISTT | GroqSTT | ElevenLabsSTT;

export interface STTOptions {
  provider: 'deepgram' | 'openai' | 'groq' | 'elevenlabs';
  deepgramApiKey?: string;
  openaiApiKey?: string;
  groqApiKey?: string;
  elevenlabsApiKey?: string;
  language?: string;
  logger?: Logger;
}

/**
 * Create an STT provider based on configuration.
 */
export function createSTT(options: STTOptions): STTProvider {
  switch (options.provider) {
    case 'deepgram':
      if (!options.deepgramApiKey) {
        throw new Error('Deepgram API key is required');
      }
      return createDeepgramSTT({
        apiKey: options.deepgramApiKey,
        language: options.language,
        logger: options.logger,
      });

    case 'openai':
      if (!options.openaiApiKey) {
        throw new Error('OpenAI API key is required');
      }
      return createOpenAISTT({
        apiKey: options.openaiApiKey,
        language: options.language,
        logger: options.logger,
      });

    case 'groq':
      if (!options.groqApiKey) {
        throw new Error('Groq API key is required');
      }
      return createGroqSTT({
        apiKey: options.groqApiKey,
        language: options.language,
        logger: options.logger,
      });

    case 'elevenlabs':
      if (!options.elevenlabsApiKey) {
        throw new Error('ElevenLabs API key is required');
      }
      return createElevenLabsSTT({
        apiKey: options.elevenlabsApiKey,
        language: options.language,
        logger: options.logger,
      });

    default:
      throw new Error(`Unknown STT provider: ${options.provider}`);
  }
}

export { DeepgramSTT, OpenAISTT, GroqSTT, ElevenLabsSTT, STTResult };
