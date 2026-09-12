import { Plugin, PluginKey, NodeSelection } from 'prosemirror-state';
import { Decoration, DecorationSet } from 'prosemirror-view';
import { Fragment, NodeType } from 'prosemirror-model';

/** @typedef {import('prosemirror-model').Node} PMNode */
/** @typedef {import('prosemirror-view').EditorView} EditorView */
/** @typedef {import('prosemirror-state').EditorState} EditorState */
/** @typedef {{fromStart:number,startMouse:{x:number,y:number},ghostEl:HTMLDivElement,active:boolean}} Dragging */
/** @typedef {{start:number,end:number,mode:string,toPos:number}} DropTarget */
/** @typedef {{decorations:DecorationSet,dragging:Dragging|null,dropTarget:DropTarget|null}} DragState */
/** @typedef {{itemTypeNames?:ReadonlyArray<string>,getEditor?:(()=>import('@tiptap/core').Editor|null)|null,
 * handleTitle?:string,handleInnerHTML?:string,classItemWithHandle?:string,classHandle?:string,
 * classDropBefore?:string,classDropAfter?:string,classDropInto?:string,classDropOutdent?:string,
 * classDraggingGhost?:string,dragThresholdPx?:number,intoThresholdX?:number,outdentThresholdX?:number}} DragOptions */

/** @type {PluginKey<DragState>} */
export const listPointerDragKey = new PluginKey('listPointerDrag');

/** @param {PMNode|NodeType} node @returns {NodeType} */
export const getListNodeType = (node) => node instanceof NodeType ? node : node.type;

/** @param {DragOptions} options */
export function listDragHandlePlugin(options = {}) {
	const {
		itemTypeNames = ['listItem', 'taskItem', 'list_item'],

		// Tiptap editor getter (required for indent/outdent)
		getEditor = null,

		// UI copy / classes
		handleTitle = 'Drag to move',
		handleInnerHTML = '⋮⋮',
		classItemWithHandle = 'pm-li--with-handle',
		classHandle = 'pm-list-drag-handle',
		classDropBefore = 'pm-li-drop-before',
		classDropAfter = 'pm-li-drop-after',
		classDropInto = 'pm-li-drop-into',
		classDropOutdent = 'pm-li-drop-outdent',
		classDraggingGhost = 'pm-li-ghost',

		// Behavior
		dragThresholdPx = 2,
		intoThresholdX = 28, // X ≥ this → treat as “into” (indent)
		outdentThresholdX = 10 // X ≤ this → “outdent”
	} = options;

	const itemTypesSet = new Set(itemTypeNames);
	/** @type {WeakMap<Element, ()=>number|undefined>} */
	const handlePositions = new WeakMap();
	/** @param {PMNode|null} node */
	const isListItem = (node) => node && itemTypesSet.has(node.type.name);

	const listTypeNames = new Set([
		'bulletList',
		'orderedList',
		'taskList',
		'bullet_list',
		'ordered_list'
	]);

	/** @param {PMNode|null} node */
	const isListNode = (node) => node && listTypeNames.has(node.type.name);

	/** @param {PMNode|NodeType} listNode */
	function listTypeToItemTypeName(listNode) {
		const name = getListNodeType(listNode).name;
		if (!name) return null;

		// Prefer tiptap names first, then ProseMirror snake_case
		if (name === 'taskList') {
			return itemTypesSet.has('taskItem') ? 'taskItem' : null;
		}
		if (name === 'orderedList' || name === 'bulletList') {
			return itemTypesSet.has('listItem')
				? 'listItem'
				: itemTypesSet.has('list_item')
					? 'list_item'
					: null;
		}
		if (name === 'ordered_list' || name === 'bullet_list') {
			return itemTypesSet.has('list_item')
				? 'list_item'
				: itemTypesSet.has('listItem')
					? 'listItem'
					: null;
		}
		return null;
	}

	// Find the nearest enclosing list container at/around a pos
	/** @param {PMNode} doc @param {number} pos */
	function getEnclosingListAt(doc, pos) {
		const $pos = doc.resolve(Math.max(1, Math.min(pos, doc.content.size - 1)));
		for (let d = $pos.depth; d >= 0; d--) {
			const n = $pos.node(d);
			if (isListNode(n)) {
				const start = $pos.before(d);
				return { node: n, depth: d, start, end: start + n.nodeSize };
			}
		}
		return null;
	}

	/** @param {EditorState} state @param {PMNode} itemNode @param {PMNode|NodeType|string} targetListNodeOrType @returns {PMNode} */
	function normalizeItemForList(state, itemNode, targetListNodeOrType) {
		const schema = state.schema;

		const targetListNode = targetListNodeOrType;
		const wantedItemTypeName =
			typeof targetListNode === 'string'
				? targetListNode // allow passing type name directly
				: listTypeToItemTypeName(targetListNode);

		if (!wantedItemTypeName) return itemNode;
		const wantedType = schema.nodes[wantedItemTypeName];
		if (!wantedType) return itemNode;

		if (typeof targetListNode === 'string') {
			return wantedType.create(itemNode.attrs, itemNode.content, itemNode.marks);
		}
		const wantedListType = getListNodeType(targetListNode);
		if (!wantedListType) return itemNode;

		// Deep‑normalize children recursively
		/** @param {PMNode} node @param {PMNode|NodeType} parentTargetListNode @returns {PMNode} */
		const normalizeNode = (node, parentTargetListNode) => {
			if (isListNode(node)) {
				// Normalize each list item inside
				/** @type {PMNode[]} */
				const normalizedItems = [];
				node.content.forEach((li) => {
					normalizedItems.push(normalizeItemForList(state, li, parentTargetListNode));
				});
				return wantedListType.create(node.attrs, Fragment.from(normalizedItems), node.marks);
			}

			// Not a list node → but may contain lists deeper
			if (node.content && node.content.size > 0) {
				/** @type {PMNode[]} */
				const nChildren = [];
				node.content.forEach((ch) => {
					nChildren.push(normalizeNode(ch, parentTargetListNode));
				});
				return node.type.create(node.attrs, Fragment.from(nChildren), node.marks);
			}

			// leaf
			return node;
		};

		/** @type {PMNode[]} */
		const normalizedContent = [];
		itemNode.content.forEach((child) => {
			normalizedContent.push(normalizeNode(child, targetListNode));
		});

		/** @type {Record<string, unknown>} */
		const newAttrs = {};
		if (wantedType.spec.attrs) {
			for (const key in wantedType.spec.attrs) {
				if (Object.prototype.hasOwnProperty.call(itemNode.attrs || {}, key)) {
					newAttrs[key] = itemNode.attrs[key];
				} else {
					const spec = wantedType.spec.attrs[key];
					newAttrs[key] = typeof spec?.default !== 'undefined' ? spec.default : null;
				}
			}
		}

		if (wantedItemTypeName !== itemNode.type.name) {
			// If changing type, ensure no disallowed marks are kept
			const allowed = wantedType.spec?.marks;
			const marks = allowed ? itemNode.marks.filter((m) => allowed.includes(m.type.name)) : [];

			return wantedType.create(newAttrs, Fragment.from(normalizedContent), marks);
		}

		try {
			return wantedType.create(newAttrs, Fragment.from(normalizedContent), itemNode.marks);
		} catch {
			// Fallback – wrap content if schema requires a block
			const para = schema.nodes.paragraph;
			if (para) {
				const wrapped =
					itemNode.content.firstChild?.type === para
						? Fragment.from(normalizedContent)
						: Fragment.from([para.create(null, normalizedContent)]);
				return wantedType.create(newAttrs, wrapped, itemNode.marks);
			}
		}

		return wantedType.create(newAttrs, Fragment.from(normalizedContent), itemNode.marks);
	}
	// ---------- decorations ----------
	/** @param {PMNode} doc */
	function buildHandleDecos(doc) {
		/** @type {Decoration[]} */
		const decos = [];
		doc.descendants((node, pos) => {
			if (!isListItem(node)) return;
			decos.push(Decoration.node(pos, pos + node.nodeSize, { class: classItemWithHandle }));
			decos.push(
				Decoration.widget(
					pos + 1,
					(view, getPos) => {
						const el = document.createElement('span');
						el.className = classHandle;
						el.setAttribute('title', handleTitle);
						el.setAttribute('role', 'button');
						el.setAttribute('aria-label', 'Drag list item');
						el.contentEditable = 'false';
						el.innerHTML = handleInnerHTML;
						handlePositions.set(el, getPos);
						return el;
					},
					{ side: -1, ignoreSelection: true }
				)
			);
		});
		return DecorationSet.create(doc, decos);
	}

	/** @param {import('prosemirror-model').ResolvedPos} $pos */
	function findListItemAround($pos) {
		for (let d = $pos.depth; d > 0; d--) {
			const node = $pos.node(d);
			if (isListItem(node)) {
				const start = $pos.before(d);
				return { depth: d, node, start, end: start + node.nodeSize };
			}
		}
		return null;
	}

	/** @param {EditorView} view @param {number} clientX @param {number} clientY */
	function infoFromCoords(view, clientX, clientY) {
		const result = view.posAtCoords({ left: clientX, top: clientY });
		if (!result) return null;
		const $pos = view.state.doc.resolve(result.pos);
		const li = findListItemAround($pos);
		if (!li) return null;

		const dom = /** @type {Element} */ (view.nodeDOM(li.start));
		if (!(dom instanceof Element)) return null;

		const rect = dom.getBoundingClientRect();
		const isRTL = getComputedStyle(dom).direction === 'rtl';
		const xFromLeft = isRTL ? rect.right - clientX : clientX - rect.left;
		const yInTopHalf = clientY - rect.top < rect.height / 2;

		const mode =
			xFromLeft <= outdentThresholdX
				? 'outdent'
				: xFromLeft >= intoThresholdX
					? 'into'
					: yInTopHalf
						? 'before'
						: 'after';

		return { ...li, dom, mode };
	}

	// ---------- state ----------
	/** @param {EditorState} state @returns {DragState} */
	const init = (state) => ({
		decorations: buildHandleDecos(state.doc),
		dragging: null, // {fromStart, startMouse:{x,y}, ghostEl, active}
		dropTarget: null // {start, end, mode, toPos}
	});

	/** @param {import('prosemirror-state').Transaction} tr @param {DragState} prev @returns {DragState} */
	const apply = (tr, prev) => {
		let decorations = tr.docChanged
			? buildHandleDecos(tr.doc)
			: prev.decorations.map(tr.mapping, tr.doc);
		let next = { ...prev, decorations };
		const meta = tr.getMeta(listPointerDragKey);
		if (meta) {
			if (meta.type === 'set-drag') next = { ...next, dragging: meta.dragging };
			if (meta.type === 'set-drop') next = { ...next, dropTarget: meta.drop };
			if (meta.type === 'clear') next = { ...next, dragging: null, dropTarget: null };
		}
		return next;
	};

	/** @param {EditorState} state */
	const decorationsProp = (state) => {
		const ps = listPointerDragKey.getState(state);
		if (!ps) return null;
		let deco = ps.decorations;
		if (ps.dropTarget) {
			const { start, end, mode } = ps.dropTarget;
			const cls =
				mode === 'before'
					? classDropBefore
					: mode === 'after'
						? classDropAfter
						: mode === 'into'
							? classDropInto
							: classDropOutdent;
			deco = deco.add(state.doc, [Decoration.node(start, end, { class: cls })]);
		}
		return deco;
	};

	// ---------- helpers ----------
	/** @param {EditorView} view @param {Dragging|null} dragging */
	const setDrag = (view, dragging) =>
		view.dispatch(view.state.tr.setMeta(listPointerDragKey, { type: 'set-drag', dragging }));
	/** @param {EditorView} view @param {DropTarget|null} drop */
	const setDrop = (view, drop) =>
		view.dispatch(view.state.tr.setMeta(listPointerDragKey, { type: 'set-drop', drop }));
	/** @param {EditorView} view */
	const clearAll = (view) =>
		view.dispatch(view.state.tr.setMeta(listPointerDragKey, { type: 'clear' }));

	/** @param {EditorView} view @param {number} fromStart @param {number} toPos */
	function moveItem(view, fromStart, toPos) {
		const { state, dispatch } = view;
		const { doc } = state;
		const orig = doc.nodeAt(fromStart);
		if (!orig || !isListItem(orig)) return { ok: false };

		// no-op if dropping into own range
		if (toPos >= fromStart && toPos <= fromStart + orig.nodeSize)
			return { ok: true, newStart: fromStart };

		// find item depth
		const $inside = doc.resolve(fromStart + 1);
		let itemDepth = -1;
		for (let d = $inside.depth; d > 0; d--) {
			if ($inside.node(d) === orig) {
				itemDepth = d;
				break;
			}
		}
		if (itemDepth < 0) return { ok: false };

		const listDepth = itemDepth - 1;
		const parentList = $inside.node(listDepth);
		const parentListStart = $inside.before(listDepth);

		// delete item (or entire list if only child)
		const deleteFrom = parentList.childCount === 1 ? parentListStart : fromStart;
		const deleteTo =
			parentList.childCount === 1
				? parentListStart + parentList.nodeSize
				: fromStart + orig.nodeSize;

		let tr = state.tr.delete(deleteFrom, deleteTo);

		// Compute mapped drop point with right bias so "after" stays after
		const mappedTo = tr.mapping.map(toPos, 1);

		// Detect enclosing list at destination, then normalize the item type
		const listAtDest = getEnclosingListAt(tr.doc, mappedTo);
		const nodeToInsert = listAtDest ? normalizeItemForList(state, orig, listAtDest.node) : orig;

		try {
			tr = tr.insert(mappedTo, nodeToInsert);
		} catch (e) {
			console.log('Direct insert failed, trying to wrap in list', e);
			// If direct insert fails (e.g., not inside a list), try wrapping in a list
			const schema = state.schema;
			const wrapName =
				parentList.type.name === 'taskList'
					? schema.nodes.taskList
						? 'taskList'
						: null
					: parentList.type.name === 'orderedList' || parentList.type.name === 'ordered_list'
						? schema.nodes.orderedList
							? 'orderedList'
							: schema.nodes.ordered_list
								? 'ordered_list'
								: null
						: schema.nodes.bulletList
							? 'bulletList'
							: schema.nodes.bullet_list
								? 'bullet_list'
								: null;

			if (wrapName) {
				const wrapType = schema.nodes[wrapName];
				if (wrapType) {
					const frag = wrapType.create(null, normalizeItemForList(state, orig, wrapType));
					tr = tr.insert(mappedTo, frag);
				} else {
					return { ok: false };
				}
			} else {
				return { ok: false };
			}
		}

		dispatch(tr.scrollIntoView());
		return { ok: true, newStart: mappedTo };
	}

	/** @param {EditorView} view @param {number} fromStart */
	function ensureGhost(view, fromStart) {
		const el = document.createElement('div');
		el.className = classDraggingGhost;
		const dom = /** @type {Element} */ (view.nodeDOM(fromStart));
		const rect = dom instanceof Element ? dom.getBoundingClientRect() : null;
		if (rect) {
			el.style.position = 'fixed';
			el.style.left = rect.left + 'px';
			el.style.top = rect.top + 'px';
			el.style.width = rect.width + 'px';
			el.style.pointerEvents = 'none';
			el.style.opacity = '0.75';
			el.textContent = dom.textContent?.trim().slice(0, 80) || '…';
		}
		document.body.appendChild(el);
		return el;
	}
	/** @param {HTMLElement|null} ghost @param {number} dx @param {number} dy */
	const updateGhost = (ghost, dx, dy) => {
		if (ghost) ghost.style.transform = `translate(${Math.round(dx)}px, ${Math.round(dy)}px)`;
	};

	// ---------- plugin ----------
	return new Plugin({
		key: listPointerDragKey,
		state: { init: (_, state) => init(state), apply },
		props: {
			decorations: decorationsProp,
			handleDOMEvents: {
				mousedown(view, event) {
					const t = /** @type {HTMLElement} */ (event.target);
					const handle = t.closest?.(`.${classHandle}`);
					if (!handle) return false;
					event.preventDefault();

					const getPos = handlePositions.get(handle);
					if (typeof getPos !== 'function') return true;

					const posInside = getPos();
					if (posInside === undefined || posInside < 1) return true;
					const fromStart = posInside - 1;

					try {
						view.dispatch(
							view.state.tr.setSelection(NodeSelection.create(view.state.doc, fromStart))
						);
					} catch {}

					const startMouse = { x: event.clientX, y: event.clientY };
					const ghostEl = ensureGhost(view, fromStart);
					setDrag(view, { fromStart, startMouse, ghostEl, active: false });

					/** @param {MouseEvent} e */
					const onMove = (e) => {
						const ps = listPointerDragKey.getState(view.state);
						if (!ps?.dragging) return;

						const dx = e.clientX - ps.dragging.startMouse.x;
						const dy = e.clientY - ps.dragging.startMouse.y;

						if (!ps.dragging.active && Math.hypot(dx, dy) > dragThresholdPx) {
							setDrag(view, { ...ps.dragging, active: true });
						}
						updateGhost(ps.dragging.ghostEl, dx, dy);

						const info = infoFromCoords(view, e.clientX, e.clientY);
						if (!info) return setDrop(view, null);

						// for before/after: obvious
						// for into/outdent: we still insert AFTER target and then run sink/lift
						const toPos =
							info.mode === 'before' ? info.start : info.mode === 'after' ? info.end : info.end; // into/outdent insert after target

						const prev = listPointerDragKey.getState(view.state)?.dropTarget;
						if (
							!prev ||
							prev.start !== info.start ||
							prev.end !== info.end ||
							prev.mode !== info.mode
						) {
							setDrop(view, { start: info.start, end: info.end, mode: info.mode, toPos });
						}
					};

					const endDrag = () => {
						window.removeEventListener('mousemove', onMove, true);
						window.removeEventListener('mouseup', endDrag, true);

						const ps = listPointerDragKey.getState(view.state);
						if (ps?.dragging?.ghostEl) ps.dragging.ghostEl.remove();

						// Helper: figure out the list item node type name at/around a pos
						/** @param {PMNode} doc @param {number} pos */
						const getListItemTypeNameAt = (doc, pos) => {
							const direct = doc.nodeAt(pos);
							if (direct && isListItem(direct)) return direct.type.name;

							const $pos = doc.resolve(Math.min(pos + 1, doc.content.size));
							for (let d = $pos.depth; d > 0; d--) {
								const n = $pos.node(d);
								if (isListItem(n)) return n.type.name;
							}

							const prefs = ['taskItem', 'listItem', 'list_item'];
							for (const p of prefs) if (itemTypesSet.has(p)) return p;
							return Array.from(itemTypesSet)[0];
						};

						if (ps?.dragging && ps?.dropTarget && ps.dragging.active) {
							const { fromStart } = ps.dragging;
							const { toPos, mode } = ps.dropTarget;

							const res = moveItem(view, fromStart, toPos);

							if (res.ok && typeof res.newStart === 'number' && getEditor) {
								const editor = getEditor();
								if (editor?.commands) {
									// Select the moved node so sink/lift applies to it
									editor.commands.setNodeSelection(res.newStart);

									const typeName = getListItemTypeNameAt(view.state.doc, res.newStart);
									const chain = editor.chain().focus();

									if (mode === 'into') {
										if (editor.can().sinkListItem?.(typeName)) chain.sinkListItem(typeName).run();
										else chain.run();
									} else {
										chain.run(); // finalize focus/selection
									}
								}
							}
						}

						clearAll(view);
					};

					window.addEventListener('mousemove', onMove, true);
					window.addEventListener('mouseup', endDrag, true);
					return true;
				},

				keydown(view, event) {
					if (event.key === 'Escape') {
						const ps = listPointerDragKey.getState(view.state);
						if (ps?.dragging?.ghostEl) ps.dragging.ghostEl.remove();
						clearAll(view);
						return true;
					}
					return false;
				}
			}
		}
	});
}
