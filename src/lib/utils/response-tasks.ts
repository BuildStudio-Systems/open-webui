type ResponseMessage = {
	role?: string;
	done?: boolean;
	childrenIds?: string[];
};

/** Completed responses must not keep the composer blocked by title/tag jobs. */
export const hasPendingAssistantResponse = (messages: Record<string, ResponseMessage>) =>
	Object.values(messages).some(
		(message) => message?.role === 'assistant' && message.done !== true && !message.childrenIds?.length
	);
