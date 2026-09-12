import { describe, expect, it } from 'vitest';
import { Schema } from 'prosemirror-model';
import { EditorState } from 'prosemirror-state';
import { getListNodeType, listDragHandlePlugin, listPointerDragKey } from './listDragHandlePlugin';

const schema = new Schema({ nodes: {
    doc: { content: 'block+' },
    paragraph: { group: 'block', content: 'text*' },
    text: {},
    bulletList: { group: 'block', content: 'listItem+' },
    listItem: { content: 'paragraph block*' }
} });
const paragraph = () => schema.nodes.paragraph.create(null, schema.text('synthetic test'));
const list = () => schema.nodes.bulletList.create(null, [schema.nodes.listItem.create(null, paragraph())]);

describe('list drag state and node normalization', () => {
    it('accepts both list nodes and NodeType fallback containers', () => {
        expect(getListNodeType(list())).toBe(schema.nodes.bulletList);
        expect(getListNodeType(schema.nodes.bulletList)).toBe(schema.nodes.bulletList);
    });
    it('creates decorations and clears an explicit drop target', () => {
        let state = EditorState.create({schema, doc: schema.nodes.doc.create(null, list()), plugins: [listDragHandlePlugin()]});
        expect(listPointerDragKey.getState(state)?.decorations.find()).toHaveLength(2);
        const drop = { start: 1, end: 3, mode: 'after', toPos: 3 };
        state = state.apply(state.tr.setMeta(listPointerDragKey, {type: 'set-drop', drop}));
        expect(listPointerDragKey.getState(state)?.dropTarget).toEqual(drop);
        state = state.apply(state.tr.setMeta(listPointerDragKey, {type: 'clear'}));
        expect(listPointerDragKey.getState(state)?.dropTarget).toBeNull();
    });
    it('does not decorate ordinary paragraphs or excluded item types', () => {
        for (const [doc, options] of [[paragraph(), {}], [list(), {itemTypeNames: ['taskItem']}]] as const) {
            const state = EditorState.create({schema, doc: schema.nodes.doc.create(null, doc), plugins: [listDragHandlePlugin(options)]});
            expect(listPointerDragKey.getState(state)?.decorations.find()).toHaveLength(0);
        }
    });
});
