export interface AuthResult {
  accessToken: string;
  trialActive: boolean;
}

export function parseAuthResponse(body: {
  access_token: string;
  trial_active?: boolean;
}): AuthResult {
  return {
    accessToken: body.access_token,
    trialActive: Boolean(body.trial_active),
  };
}
