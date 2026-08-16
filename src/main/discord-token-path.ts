import { join } from 'path'

/** Keep development and installed-app secrets bound to their own Chromium key stores. */
export function discordTokenPath(userData: string, isPackaged: boolean): string {
  return join(userData, isPackaged ? 'discord.token.enc' : 'discord.token.dev.enc')
}

export function hasUsableDiscordToken(token: string): boolean {
  return token.trim().length > 0
}
