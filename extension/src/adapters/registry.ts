import { FundingPipsBrowserAdapter } from './fundingpips/adapter';
import { TradingPlatformAdapter } from '../types/adapter';

const adapters: TradingPlatformAdapter[] = [new FundingPipsBrowserAdapter()];

export const adapterRegistry = {
  list(): TradingPlatformAdapter[] {
    return adapters;
  },
  async detectForUrl(url: string): Promise<TradingPlatformAdapter | null> {
    for (const adapter of adapters) {
      if (await adapter.detect(url)) return adapter;
    }
    return null;
  },
};
