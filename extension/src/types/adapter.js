/** @typedef {{canReadState:boolean,canPlaceOrder:boolean,canClosePosition:boolean,canModifyPosition:boolean,canCancelOrder:boolean}} AdapterCapability */

export class AdapterError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}
