export interface Finding { rule: string; file?: string }
export function scanText(text: string, forbidden?: string[]): Finding[];
export function scanDir(dir: string, forbidden?: string[]): Finding[];
