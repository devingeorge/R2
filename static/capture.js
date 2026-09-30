class CaptureProcessor extends AudioWorkletProcessor {
  constructor() { super(); this.frame = new Int16Array(1280); this.offset = 0; }
  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true;
    for (const sample of channel) {
      this.frame[this.offset++] = Math.round(Math.max(-1, Math.min(1, sample)) * 32767);
      if (this.offset === 1280) {
        this.port.postMessage(this.frame.buffer, [this.frame.buffer]);
        this.frame = new Int16Array(1280); this.offset = 0;
      }
    }
    return true;
  }
}
registerProcessor('r2-capture', CaptureProcessor);

