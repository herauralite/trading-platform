export type AdapterCapability = {
  canReadState: boolean;
  canPlaceOrder: boolean;
  canClosePosition: boolean;
  canModifyPosition: boolean;
  canCancelOrder: boolean;
};

export type NormalizedAccountState = {
  platformAccountRef: string;
  balance?: number;
  equity?: number;
  drawdown?: number;
  riskUsed?: number;
};

export type NormalizedPosition = {
  symbol: string;
  side: 'buy' | 'sell';
  size?: number;
  averageEntry?: number;
  unrealizedPnl?: number;
  stopLoss?: number;
  takeProfit?: number;
  openedAt?: string;
};

export type NormalizedOrder = {
  platformOrderRef: string;
  symbol: string;
  side: 'buy' | 'sell';
  orderType: string;
  status: string;
  quantity?: number;
  filledQuantity?: number;
  price?: number;
  stopPrice?: number;
  submittedAt?: string;
};

export type ExecutionRequest = {
  commandType: 'place_order' | 'close_position' | 'modify_position' | 'cancel_order';
  payload: Record<string, unknown>;
};

export type ExecutionResult = {
  status: 'succeeded' | 'failed';
  data?: Record<string, unknown>;
  errorCode?: string;
  errorMessage?: string;
};

export class AdapterError extends Error {
  constructor(public code: string, message: string) {
    super(message);
  }
}

export interface TradingPlatformAdapter {
  adapterKey: string;
  platformKey: string;
  capabilities: AdapterCapability;
  detect(url: string, documentTitle?: string): Promise<boolean>;
  readAccountState(): Promise<NormalizedAccountState>;
  readPositions(): Promise<NormalizedPosition[]>;
  readOrders(): Promise<NormalizedOrder[]>;
  placeOrder(request: ExecutionRequest): Promise<ExecutionResult>;
  closePosition(request: ExecutionRequest): Promise<ExecutionResult>;
  modifyPosition(request: ExecutionRequest): Promise<ExecutionResult>;
  cancelOrder(request: ExecutionRequest): Promise<ExecutionResult>;
}
