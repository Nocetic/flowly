#!/usr/bin/env node
/**
 * Flowly Voice Bridge
 *
 * Real-time voice call bridge using Twilio Media Streams,
 * with Deepgram/OpenAI for STT and OpenAI for TTS.
 *
 * Usage:
 *   npm run build && npm start
 *
 * Environment variables:
 *   See .env.example for required configuration.
 */

import pino from 'pino';
import { loadConfig, validateProviderConfig, type Config } from './config.js';
import { createCallManager, CallManager } from './call-manager.js';
import { createServer, VoiceBridgeServer } from './server.js';

// ASCII art logo
const LOGO = `
╔═══════════════════════════════════════╗
║  📞 Flowly Voice Bridge               ║
║  Real-time AI Voice Conversations     ║
╚═══════════════════════════════════════╝
`;

async function main(): Promise<void> {
  console.log(LOGO);

  // Load and validate configuration
  let config: Config;
  try {
    config = loadConfig();
    validateProviderConfig(config);
  } catch (error: any) {
    console.error('❌ Configuration error:', error.message);
    console.error('\nMake sure to set required environment variables.');
    console.error('See .env.example for details.');
    process.exit(1);
  }

  // Create logger
  const logger = pino({
    level: config.logLevel,
    transport: {
      target: 'pino-pretty',
      options: {
        colorize: true,
      },
    },
  });

  logger.info('Starting voice bridge...');
  logger.info({ sttProvider: config.stt.provider, ttsProvider: config.tts.provider }, 'Providers');

  // Create call manager
  const callManager = createCallManager({
    config,
    logger,
  });

  // Create server
  const server = createServer({
    config,
    callManager,
    logger,
  });

  // Set up transcript handler (forward to Flowly agent)
  server.setTranscriptHandler(async (callSid, text) => {
    logger.info({ callSid, text }, 'Transcript received, forwarding to Flowly');

    try {
      const gatewayUrl = config.flowly.gatewayUrl;
      const call = callManager.getCall(callSid);

      const response = await fetch(`${gatewayUrl}/api/voice/message`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          call_sid: callSid,
          from: call?.from || 'unknown',
          text: text,
        }),
      });

      if (!response.ok) {
        throw new Error(`Flowly returned ${response.status}`);
      }

      const data = await response.json() as { response?: string };

      if (data.response) {
        logger.info({ callSid, response: data.response.substring(0, 50) }, 'Got agent response');
        return data.response;
      }

      return undefined;
    } catch (error) {
      logger.error({ error, callSid }, 'Failed to forward to Flowly');
      return "Üzgünüm, bir hata oluştu. Lütfen tekrar deneyin.";
    }
  });

  // Handle graceful shutdown
  const shutdown = async () => {
    logger.info('Shutting down...');
    await server.stop();
    process.exit(0);
  };

  // Cross-platform signal handling
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
  // Windows-specific: handle when the console window is closed
  if (process.platform === 'win32') {
    process.on('SIGHUP', shutdown);
  }

  // Start server
  try {
    await server.start();

    const { host, port } = config.webhook;
    console.log(`
✅ Voice bridge is running!

Webhook endpoints:
  POST /voice/inbound  - Handle incoming calls
  POST /voice/status   - Call status updates
  POST /voice/gather   - Speech/DTMF input
  WS   /voice/stream   - Media Streams WebSocket

API endpoints:
  POST /api/call       - Make a call
  POST /api/speak      - Speak on a call
  POST /api/end        - End a call
  GET  /api/call/:sid  - Get call details
  GET  /api/calls      - List active calls
  GET  /health         - Health check

Listening on: http://${host}:${port}

To receive calls, configure Twilio webhook URL:
  ${config.webhook.baseUrl || `http://localhost:${port}`}/voice/inbound

For public access, use ngrok:
  ngrok http ${port}
`);
  } catch (error) {
    logger.fatal({ error }, 'Failed to start server');
    process.exit(1);
  }
}

// Run
main().catch((error) => {
  console.error('Fatal error:', error);
  process.exit(1);
});
