/**
 * Configuration schema and loader for voice bridge.
 */

import { z } from 'zod';
import { config as dotenvConfig } from 'dotenv';

// Load .env file
dotenvConfig();

export const ConfigSchema = z.object({
  // Twilio
  twilio: z.object({
    accountSid: z.string().min(1, 'TWILIO_ACCOUNT_SID is required'),
    authToken: z.string().min(1, 'TWILIO_AUTH_TOKEN is required'),
    phoneNumber: z.string().min(1, 'TWILIO_PHONE_NUMBER is required'),
  }),

  // Webhook server
  webhook: z.object({
    baseUrl: z.string().url().optional(),
    port: z.number().default(8765),
    host: z.string().default('0.0.0.0'),
  }),

  // STT (Speech-to-Text)
  stt: z.object({
    provider: z.enum(['deepgram', 'openai', 'groq', 'elevenlabs']).default('deepgram'),
    deepgramApiKey: z.string().optional(),
    openaiApiKey: z.string().optional(),
    groqApiKey: z.string().optional(),
    elevenlabsApiKey: z.string().optional(),
    language: z.string().default('en-US'),
  }),

  // TTS (Text-to-Speech)
  tts: z.object({
    provider: z.enum(['openai', 'deepgram', 'elevenlabs']).default('openai'),
    openaiApiKey: z.string().optional(),
    deepgramApiKey: z.string().optional(),
    elevenlabsApiKey: z.string().optional(),
    voice: z.string().default('nova'),
    model: z.string().default('tts-1'),
  }),

  // Flowly agent connection
  flowly: z.object({
    gatewayUrl: z.string().url().default('http://localhost:18790'),
  }),

  // Logging
  logLevel: z.enum(['trace', 'debug', 'info', 'warn', 'error', 'fatal']).default('info'),
});

export type Config = z.infer<typeof ConfigSchema>;

/**
 * Load and validate configuration from environment variables.
 */
export function loadConfig(): Config {
  const rawConfig = {
    twilio: {
      accountSid: process.env.TWILIO_ACCOUNT_SID || '',
      authToken: process.env.TWILIO_AUTH_TOKEN || '',
      phoneNumber: process.env.TWILIO_PHONE_NUMBER || '',
    },
    webhook: {
      baseUrl: process.env.WEBHOOK_BASE_URL,
      port: parseInt(process.env.WEBHOOK_PORT || '8765', 10),
      host: process.env.WEBHOOK_HOST || '0.0.0.0',
    },
    stt: {
      provider: process.env.STT_PROVIDER || 'deepgram',
      deepgramApiKey: process.env.DEEPGRAM_API_KEY,
      openaiApiKey: process.env.OPENAI_API_KEY,
      groqApiKey: process.env.GROQ_API_KEY,
      elevenlabsApiKey: process.env.ELEVENLABS_API_KEY,
      language: process.env.LANGUAGE || 'en-US',
    },
    tts: {
      provider: process.env.TTS_PROVIDER || 'openai',
      openaiApiKey: process.env.OPENAI_API_KEY,
      deepgramApiKey: process.env.DEEPGRAM_API_KEY,
      elevenlabsApiKey: process.env.ELEVENLABS_API_KEY,
      voice: process.env.TTS_VOICE || 'nova',
      model: process.env.TTS_MODEL || 'tts-1',
    },
    flowly: {
      gatewayUrl: process.env.FLOWLY_GATEWAY_URL || 'http://localhost:18790',
    },
    logLevel: process.env.LOG_LEVEL || 'info',
  };

  return ConfigSchema.parse(rawConfig);
}

/**
 * Validate that required API keys are present for the selected providers.
 */
export function validateProviderConfig(config: Config): void {
  if (config.stt.provider === 'deepgram' && !config.stt.deepgramApiKey) {
    throw new Error('DEEPGRAM_API_KEY is required when STT_PROVIDER=deepgram');
  }

  if (config.stt.provider === 'openai' && !config.stt.openaiApiKey) {
    throw new Error('OPENAI_API_KEY is required when STT_PROVIDER=openai');
  }

  if (config.stt.provider === 'groq' && !config.stt.groqApiKey) {
    throw new Error('GROQ_API_KEY is required when STT_PROVIDER=groq');
  }

  if (config.tts.provider === 'openai' && !config.tts.openaiApiKey) {
    throw new Error('OPENAI_API_KEY is required when TTS_PROVIDER=openai');
  }

  if (config.tts.provider === 'deepgram' && !config.tts.deepgramApiKey) {
    throw new Error('DEEPGRAM_API_KEY is required when TTS_PROVIDER=deepgram');
  }

  if (config.stt.provider === 'elevenlabs' && !config.stt.elevenlabsApiKey) {
    throw new Error('ELEVENLABS_API_KEY is required when STT_PROVIDER=elevenlabs');
  }

  if (config.tts.provider === 'elevenlabs' && !config.tts.elevenlabsApiKey) {
    throw new Error('ELEVENLABS_API_KEY is required when TTS_PROVIDER=elevenlabs');
  }
}
