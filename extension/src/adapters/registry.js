import { FundingPipsBrowserAdapter } from './fundingpips/adapter.js';

const adapters = [new FundingPipsBrowserAdapter()];

export const adapterRegistry = {
  list() {
    return adapters;
  },
  async detectForUrl(url) {
    for (const adapter of adapters) {
      if (await adapter.detect(url)) return adapter;
    }
    return null;
  },
};
