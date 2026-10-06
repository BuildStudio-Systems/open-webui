export function prepareShareChat(options: {
 file: File; token: string; userId: string; current: () => boolean;
 request: (path: string, options: {token: string; method?: string; body?: FormData|string}) => Promise<any>;
 storage: Pick<Storage,'setItem'>; prompt: string; modelIds?: string[];
}): Promise<{chatId:string;fileId:string}>;
