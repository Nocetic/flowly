/**
 * TwiML (Twilio Markup Language) generation utilities.
 *
 * TwiML is XML that instructs Twilio how to handle calls.
 */

export type GatherInput = 'dtmf' | 'speech' | 'dtmf speech';

/**
 * TwiML builder with fluent interface.
 */
export class TwiMLBuilder {
  private elements: string[] = [];
  private gatherContent: string[] | null = null;
  private connectContent: string[] | null = null;

  /**
   * Add a Say verb to speak text.
   */
  say(
    text: string,
    options: {
      voice?: string;
      language?: string;
      loop?: number;
    } = {}
  ): this {
    const { voice = 'Polly.Joanna', language = 'en-US', loop } = options;

    let attrs = `voice="${voice}" language="${language}"`;
    if (loop !== undefined && loop !== 1) {
      attrs += ` loop="${loop}"`;
    }

    const element = `<Say ${attrs}>${this.escapeXml(text)}</Say>`;
    this.addElement(element);
    return this;
  }

  /**
   * Add a Play verb to play audio.
   */
  play(url: string, options: { loop?: number } = {}): this {
    const { loop } = options;
    let attrs = '';
    if (loop !== undefined && loop !== 1) {
      attrs = ` loop="${loop}"`;
    }

    const element = `<Play${attrs}>${this.escapeXml(url)}</Play>`;
    this.addElement(element);
    return this;
  }

  /**
   * Add a Pause verb.
   */
  pause(length: number = 1): this {
    const element = `<Pause length="${length}"/>`;
    this.addElement(element);
    return this;
  }

  /**
   * Start a Gather verb to collect input.
   */
  gather(options: {
    action?: string;
    method?: string;
    input?: GatherInput;
    timeout?: number;
    speechTimeout?: string;
    numDigits?: number;
    finishOnKey?: string;
    language?: string;
    hints?: string;
    profanityFilter?: boolean;
  } = {}): this {
    const {
      action,
      method = 'POST',
      input = 'dtmf speech',
      timeout = 5,
      speechTimeout = 'auto',
      numDigits,
      finishOnKey = '#',
      language = 'en-US',
      hints,
      profanityFilter,
    } = options;

    let attrs = `input="${input}" method="${method}" timeout="${timeout}" speechTimeout="${speechTimeout}" language="${language}" finishOnKey="${finishOnKey}"`;

    if (action) attrs += ` action="${this.escapeXml(action)}"`;
    if (numDigits) attrs += ` numDigits="${numDigits}"`;
    if (hints) attrs += ` hints="${this.escapeXml(hints)}"`;
    if (profanityFilter) attrs += ` profanityFilter="true"`;

    this.gatherContent = [];
    this.elements.push(`<Gather ${attrs}>`);
    return this;
  }

  /**
   * End the current Gather context.
   */
  endGather(): this {
    if (this.gatherContent !== null) {
      this.elements.push(...this.gatherContent);
      this.gatherContent = null;
    }
    this.elements.push('</Gather>');
    return this;
  }

  /**
   * Add a Record verb.
   */
  record(options: {
    action?: string;
    method?: string;
    timeout?: number;
    finishOnKey?: string;
    maxLength?: number;
    transcribe?: boolean;
    transcribeCallback?: string;
    playBeep?: boolean;
  } = {}): this {
    const {
      action,
      method = 'POST',
      timeout = 10,
      finishOnKey = '#',
      maxLength = 3600,
      transcribe,
      transcribeCallback,
      playBeep = true,
    } = options;

    let attrs = `method="${method}" timeout="${timeout}" finishOnKey="${finishOnKey}" maxLength="${maxLength}" playBeep="${playBeep}"`;

    if (action) attrs += ` action="${this.escapeXml(action)}"`;
    if (transcribe) {
      attrs += ` transcribe="true"`;
      if (transcribeCallback) {
        attrs += ` transcribeCallback="${this.escapeXml(transcribeCallback)}"`;
      }
    }

    const element = `<Record ${attrs}/>`;
    this.addElement(element);
    return this;
  }

  /**
   * Add a Dial verb.
   */
  dial(
    number: string | null,
    options: {
      action?: string;
      method?: string;
      timeout?: number;
      callerId?: string;
      record?: string;
    } = {}
  ): this {
    const { action, method = 'POST', timeout = 30, callerId, record } = options;

    let attrs = `method="${method}" timeout="${timeout}"`;
    if (action) attrs += ` action="${this.escapeXml(action)}"`;
    if (callerId) attrs += ` callerId="${callerId}"`;
    if (record) attrs += ` record="${record}"`;

    if (number) {
      this.elements.push(`<Dial ${attrs}>${this.escapeXml(number)}</Dial>`);
    } else {
      this.elements.push(`<Dial ${attrs}/>`);
    }
    return this;
  }

  /**
   * Add a Hangup verb.
   */
  hangup(): this {
    this.addElement('<Hangup/>');
    return this;
  }

  /**
   * Add a Redirect verb.
   */
  redirect(url: string, method: string = 'POST'): this {
    const element = `<Redirect method="${method}">${this.escapeXml(url)}</Redirect>`;
    this.addElement(element);
    return this;
  }

  /**
   * Add a Reject verb.
   */
  reject(reason: 'rejected' | 'busy' = 'rejected'): this {
    const element = `<Reject reason="${reason}"/>`;
    this.addElement(element);
    return this;
  }

  /**
   * Start a Connect verb for Media Streams.
   */
  connect(): this {
    this.connectContent = [];
    this.elements.push('<Connect>');
    return this;
  }

  /**
   * Add a Stream inside Connect.
   */
  stream(options: {
    url: string;
    name?: string;
    track?: 'inbound_track' | 'outbound_track' | 'both_tracks';
  }): this {
    const { url, name, track = 'both_tracks' } = options;

    let attrs = `url="${this.escapeXml(url)}" track="${track}"`;
    if (name) attrs += ` name="${this.escapeXml(name)}"`;

    const element = `<Stream ${attrs}/>`;

    if (this.connectContent !== null) {
      this.connectContent.push(element);
    } else {
      this.addElement(element);
    }
    return this;
  }

  /**
   * End the Connect context.
   */
  endConnect(): this {
    if (this.connectContent !== null) {
      this.elements.push(...this.connectContent);
      this.connectContent = null;
    }
    this.elements.push('</Connect>');
    return this;
  }

  /**
   * Build the TwiML string.
   */
  build(): string {
    return `<?xml version="1.0" encoding="UTF-8"?><Response>${this.elements.join('')}</Response>`;
  }

  /**
   * Add element to current context (gather, connect, or root).
   */
  private addElement(element: string): void {
    if (this.gatherContent !== null) {
      this.gatherContent.push(element);
    } else if (this.connectContent !== null) {
      this.connectContent.push(element);
    } else {
      this.elements.push(element);
    }
  }

  /**
   * Escape XML special characters.
   */
  private escapeXml(text: string): string {
    return text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&apos;');
  }
}

// Convenience functions

/**
 * Generate simple Say TwiML.
 */
export function sayTwiML(
  text: string,
  voice: string = 'Polly.Joanna',
  language: string = 'en-US'
): string {
  return new TwiMLBuilder().say(text, { voice, language }).build();
}

/**
 * Generate TwiML for gathering speech input.
 */
export function gatherSpeechTwiML(
  prompt: string,
  action: string,
  options: {
    voice?: string;
    language?: string;
    timeout?: number;
    hints?: string;
  } = {}
): string {
  const { voice = 'Polly.Joanna', language = 'en-US', timeout = 5, hints } = options;

  return new TwiMLBuilder()
    .gather({
      action,
      input: 'speech',
      timeout,
      language,
      hints,
    })
    .say(prompt, { voice, language })
    .endGather()
    .build();
}

/**
 * Generate hangup TwiML.
 */
export function hangupTwiML(): string {
  return new TwiMLBuilder().hangup().build();
}

/**
 * Generate TwiML for Media Streams (bidirectional audio).
 */
export function mediaStreamTwiML(
  websocketUrl: string,
  track: 'inbound_track' | 'outbound_track' | 'both_tracks' = 'both_tracks'
): string {
  return new TwiMLBuilder()
    .connect()
    .stream({ url: websocketUrl, track })
    .endConnect()
    .build();
}
