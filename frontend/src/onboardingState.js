import { deriveAccountConnectionState } from './accountConnectionState.js'

export function deriveAppOnboardingState({
  signedIn,
  useWorkspaceApi,
  workspaceApiHydrated,
  workspaceApiAccounts = [],
  fallbackAccounts = [],
}) {
  if (!signedIn) {
    return {
      accountConnectionState: deriveAccountConnectionState([]),
      hasZeroUsableAccounts: false,
      source: 'signed_out',
    }
  }

  if (useWorkspaceApi && workspaceApiHydrated) {
    const connectionState = deriveAccountConnectionState(workspaceApiAccounts)
    return {
      accountConnectionState: connectionState,
      hasZeroUsableAccounts: connectionState.hasZeroConnectedAccounts,
      source: 'workspace_api',
    }
  }

  const fallbackConnectionState = deriveAccountConnectionState(fallbackAccounts)
  return {
    accountConnectionState: fallbackConnectionState,
    hasZeroUsableAccounts: fallbackConnectionState.hasZeroConnectedAccounts,
    source: useWorkspaceApi ? 'connector_overview_fallback' : 'connector_overview',
  }
}
