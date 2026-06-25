/**
 * Fastify server for handling Twilio webhooks and WebSocket connections.
 */

import Fastify, { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import fastifyWebsocket from '@fastify/websocket';
import fastifyFormbody from '@fastify/formbody';
// Generic WebSocket type - Fastify WebSocket has different API than 'ws'
type GenericWebSocket = any;
import { Logger } from 'pino';
import { CallManager } from './call-manager.js';
import type { Config } from './config.js';
import type { CallEvent } from './types.js';

export interface ServerOptions {
  config: Config;
  callManager: CallManager;
  logger?: Logger;
  onTranscript?: (callSid: string, text: string) => Promise<string | void>;
}

export class VoiceBridgeServer {
  private app: FastifyInstance;
  private config: Config;
  private callManager: CallManager;
  private logger?: Logger;
  private onTranscript?: (callSid: string, text: string) => Promise<string | void>;

  private initialized: Promise<void>;

  constructor(options: ServerOptions) {
    this.config = options.config;
    this.callManager = options.callManager;
    this.logger = options.logger;
    this.onTranscript = options.onTranscript;

    this.app = Fastify({
      logger: this.logger ? { level: this.config.logLevel } : false,
    });

    // Initialize async - plugins must be registered before routes
    this.initialized = this.initialize();
  }

  private async initialize(): Promise<void> {
    await this.setupPlugins();
    this.setupRoutes();
    this.setupCallManagerListeners();
  }

  /**
   * Set up Fastify plugins.
   */
  private async setupPlugins(): Promise<void> {
    await this.app.register(fastifyWebsocket);
    await this.app.register(fastifyFormbody);
  }

  /**
   * Set up HTTP routes.
   */
  private setupRoutes(): void {
    // Health check
    this.app.get('/health', async () => {
      return {
        status: 'ok',
        service: 'voice-bridge',
        activeCalls: this.callManager.getActiveCalls().length,
      };
    });

    // Twilio webhooks
    this.app.post('/voice/inbound', this.handleInbound.bind(this));
    this.app.post('/voice/status', this.handleStatus.bind(this));
    this.app.post('/voice/gather', this.handleGather.bind(this));

    // WebSocket for Media Streams
    // @fastify/websocket passes the ws WebSocket directly as first parameter
    this.app.get('/voice/stream', { websocket: true }, (socket: GenericWebSocket, request) => {
      this.logger?.info('WebSocket connection established for media stream');
      this.handleStream(socket, request);
    });

    // API endpoints for agent control
    this.app.post('/api/call', this.handleApiCall.bind(this));
    this.app.post('/api/speak', this.handleApiSpeak.bind(this));
    this.app.post('/api/end', this.handleApiEnd.bind(this));
    this.app.get('/api/call/:callSid', this.handleApiGetCall.bind(this));
    this.app.get('/api/calls', this.handleApiListCalls.bind(this));
  }

  /**
   * Set up call manager event listeners.
   */
  private setupCallManagerListeners(): void {
    // Handle transcription events
    this.callManager.on('transcription', async (event: CallEvent) => {
      if (this.onTranscript && event.data.text) {
        try {
          const response = await this.onTranscript(event.callSid, event.data.text as string);
          if (response) {
            await this.callManager.speak(event.callSid, response);
          }
        } catch (error) {
          this.logger?.error({ error, callSid: event.callSid }, 'Error handling transcript');
        }
      }
    });
  }

  /**
   * Verify Twilio signature middleware.
   */
  private verifySignature(
    request: FastifyRequest,
    reply: FastifyReply,
    params: Record<string, string>
  ): boolean {
    const signature = request.headers['x-twilio-signature'] as string;

    if (!signature) {
      this.logger?.warn('Missing Twilio signature header');
      // Allow in development
      if (!this.config.webhook.baseUrl) {
        return true;
      }
      reply.status(403).send('Invalid signature');
      return false;
    }

    // Build full URL
    let url = `${request.protocol}://${request.hostname}${request.url}`;

    // Use configured base URL if set
    if (this.config.webhook.baseUrl) {
      url = `${this.config.webhook.baseUrl}${request.url}`;
    }

    if (!this.callManager.verifySignature(signature, url, params)) {
      this.logger?.warn('Invalid Twilio signature');
      reply.status(403).send('Invalid signature');
      return false;
    }

    return true;
  }

  /**
   * Handle inbound call webhook.
   */
  private async handleInbound(
    request: FastifyRequest<{ Body: Record<string, string> }>,
    reply: FastifyReply
  ): Promise<void> {
    const params = request.body;

    if (!this.verifySignature(request, reply, params)) {
      return;
    }

    try {
      const greeting = 'Hello, this is Flowly. How can I help you?';
      const twiml = await this.callManager.handleInboundCall(params, greeting);

      reply.type('application/xml').send(twiml);
    } catch (error) {
      this.logger?.error({ error }, 'Inbound call error');
      reply
        .type('application/xml')
        .send(
          '<Response><Say>Sorry, an error occurred. Please try again later.</Say><Hangup/></Response>'
        );
    }
  }

  /**
   * Handle status callback webhook.
   */
  private async handleStatus(
    request: FastifyRequest<{ Body: Record<string, string> }>,
    reply: FastifyReply
  ): Promise<void> {
    const params = request.body;

    if (!this.verifySignature(request, reply, params)) {
      return;
    }

    try {
      await this.callManager.handleStatusCallback(params);
      reply.send('');
    } catch (error) {
      this.logger?.error({ error }, 'Status callback error');
      reply.status(500).send('Error');
    }
  }

  /**
   * Handle gather callback webhook.
   */
  private async handleGather(
    request: FastifyRequest<{ Body: Record<string, string> }>,
    reply: FastifyReply
  ): Promise<void> {
    const params = request.body;

    if (!this.verifySignature(request, reply, params)) {
      return;
    }

    try {
      const twiml = await this.callManager.handleGatherCallback(params);
      reply.type('application/xml').send(twiml);
    } catch (error) {
      this.logger?.error({ error }, 'Gather callback error');
      reply
        .type('application/xml')
        .send(
          '<Response><Say>Sorry, I did not understand that.</Say><Hangup/></Response>'
        );
    }
  }

  /**
   * Handle WebSocket connection for Media Streams.
   */
  private async handleStream(
    socket: GenericWebSocket,
    _request: FastifyRequest
  ): Promise<void> {
    this.logger?.info('Media stream WebSocket connected');

    try {
      await this.callManager.handleMediaStream(socket);
    } catch (error: any) {
      this.logger?.error({
        error: error?.message || error,
        stack: error?.stack,
        name: error?.name
      }, 'Media stream error');
      if (typeof socket.close === 'function') {
        socket.close();
      } else if (typeof socket.terminate === 'function') {
        socket.terminate();
      }
    }
  }

  /**
   * API: Make a call.
   */
  private async handleApiCall(
    request: FastifyRequest<{
      Body: {
        to: string;
        message?: string;
        greeting?: string;
        conversation?: boolean;
        metadata?: Record<string, unknown>;
      };
    }>,
    reply: FastifyReply
  ): Promise<void> {
    try {
      const { to, message, greeting, conversation, metadata } = request.body;

      let call;
      if (conversation || greeting) {
        call = await this.callManager.makeConversationCall({
          to,
          greeting: greeting || message || 'Hello!',
          metadata,
        });
      } else if (message) {
        call = await this.callManager.makeCall({
          to,
          message,
          metadata,
        });
      } else {
        reply.status(400).send({ error: 'message or greeting required' });
        return;
      }

      reply.send({
        success: true,
        callSid: call.callSid,
        state: call.state,
      });
    } catch (error: any) {
      this.logger?.error({ error }, 'API call error');
      reply.status(500).send({ error: error.message });
    }
  }

  /**
   * API: Speak on a call.
   */
  private async handleApiSpeak(
    request: FastifyRequest<{
      Body: {
        callSid: string;
        message: string;
      };
    }>,
    reply: FastifyReply
  ): Promise<void> {
    try {
      const { callSid, message } = request.body;

      await this.callManager.speak(callSid, message);

      reply.send({ success: true });
    } catch (error: any) {
      this.logger?.error({ error }, 'API speak error');
      reply.status(500).send({ error: error.message });
    }
  }

  /**
   * API: End a call.
   */
  private async handleApiEnd(
    request: FastifyRequest<{
      Body: {
        callSid: string;
        message?: string;
      };
    }>,
    reply: FastifyReply
  ): Promise<void> {
    try {
      const { callSid, message } = request.body;

      await this.callManager.endCall(callSid, message);

      reply.send({ success: true });
    } catch (error: any) {
      this.logger?.error({ error }, 'API end call error');
      reply.status(500).send({ error: error.message });
    }
  }

  /**
   * API: Get call details.
   */
  private async handleApiGetCall(
    request: FastifyRequest<{
      Params: { callSid: string };
    }>,
    reply: FastifyReply
  ): Promise<void> {
    const { callSid } = request.params;
    const call = this.callManager.getCall(callSid);

    if (!call) {
      reply.status(404).send({ error: 'Call not found' });
      return;
    }

    reply.send(call);
  }

  /**
   * API: List active calls.
   */
  private async handleApiListCalls(
    _request: FastifyRequest,
    reply: FastifyReply
  ): Promise<void> {
    const calls = this.callManager.getActiveCalls();
    reply.send({ calls });
  }

  /**
   * Set transcript handler.
   */
  setTranscriptHandler(handler: (callSid: string, text: string) => Promise<string | void>): void {
    this.onTranscript = handler;
  }

  /**
   * Start the server.
   */
  async start(): Promise<void> {
    // Wait for initialization to complete
    await this.initialized;

    const { host, port } = this.config.webhook;

    try {
      await this.app.listen({ host, port });
      this.logger?.info({ host, port }, 'Voice bridge server started');
    } catch (error) {
      this.logger?.error({ error }, 'Failed to start server');
      throw error;
    }
  }

  /**
   * Stop the server.
   */
  async stop(): Promise<void> {
    await this.callManager.cleanup();
    await this.app.close();
    this.logger?.info('Voice bridge server stopped');
  }
}

/**
 * Create a voice bridge server instance.
 */
export function createServer(options: ServerOptions): VoiceBridgeServer {
  return new VoiceBridgeServer(options);
}
