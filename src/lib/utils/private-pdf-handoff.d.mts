export const SHARE_ORIGIN: string;
export const THERE_ORIGIN: string;
export const MAX_PDF: number;
export function validMetadata(value: unknown): boolean;
export function readBoundedPdf(response: Response, signal: AbortSignal): Promise<ArrayBuffer>;
export function createPdfSender(options: {win: Window; peer: Window; channel: string; metadata: {name:string;size:number;type:string}; load:(signal:AbortSignal)=>Promise<ArrayBuffer>; onState?:(state:string)=>void; ttl?:number}): {cancel():void};
export function createPdfReceiver(options: {win: Window; peer: Window; channel: string; onOffer:(metadata:{name:string;size:number;type:string})=>void; receive:(file:File,current:()=>boolean)=>Promise<void>; onState?:(state:string)=>void; ttl?:number}): {accept():void;cancel():void};
