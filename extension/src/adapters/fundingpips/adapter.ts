import {
  AdapterError,
  ExecutionRequest,
  ExecutionResult,
  NormalizedAccountState,
  NormalizedOrder,
  NormalizedPosition,
  TradingPlatformAdapter,
} from '../../types/adapter';
import { FUNDINGPIPS_HOST_MATCH } from './selectors';

export class FundingPipsBrowserAdapter implements TradingPlatformAdapter {
  adapterKey = 'fundingpips_browser';
  platformKey = 'fundingpips';
  capabilities = {
    canReadState: true,
    canPlaceOrder: true,
    canClosePosition: true,
    canModifyPosition: true,
    canCancelOrder: true,
  };

  async detect(url: string): Promise<boolean> {
    return FUNDINGPIPS_HOST_MATCH.some((host) => url.includes(host));
  }

  async readAccountState(): Promise<NormalizedAccountState> {
    // TODO: map DOM/session values into normalized account snapshot.
    return { platformAccountRef: 'unknown' };
  }

  async readPositions(): Promise<NormalizedPosition[]> {
    // TODO: implement robust DOM extraction with retry and stale-node protection.
    return [];
  }

  async readOrders(): Promise<NormalizedOrder[]> {
    // TODO: parse open/pending orders from platform DOM and normalize statuses.
    return [];
  }

  async placeOrder(_request: ExecutionRequest): Promise<ExecutionResult> {
    throw new AdapterError('not_implemented', 'placeOrder wiring pending selector finalization');
  }

  async closePosition(_request: ExecutionRequest): Promise<ExecutionResult> {
    throw new AdapterError('not_implemented', 'closePosition wiring pending selector finalization');
  }

  async modifyPosition(_request: ExecutionRequest): Promise<ExecutionResult> {
    throw new AdapterError('not_implemented', 'modifyPosition wiring pending selector finalization');
  }

  async cancelOrder(_request: ExecutionRequest): Promise<ExecutionResult> {
    throw new AdapterError('not_implemented', 'cancelOrder wiring pending selector finalization');
  }
}
