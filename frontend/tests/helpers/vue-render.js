import { readFileSync } from 'node:fs'
import * as Vue from 'vue'
import { parse, compileScript } from 'vue/compiler-sfc'

// Compile the real SFC with Vue's bundled compiler, without a browser or test dependency.
export function loadComponent(url, imports = {}) {
  let code = compileScript(parse(readFileSync(url, 'utf8')).descriptor, { id: 'test', inlineTemplate: true }).content
  code = code.replace(/import \{([^}]+)\} from ["']([^"']+)["'];?/g,
    (_, names, path) => `const {${names.replace(/\bas\b/g, ':')}} = imports[${JSON.stringify(path)}]`)
  return new Function('imports', code.replace('export default', 'return'))({ vue: Vue, ...imports })
}

export function mount(component, props, slots) {
  const node = (tag, text = '') => ({ tag, text, props: {}, children: [], parent: null })
  const renderer = Vue.createRenderer({
    createElement: tag => node(tag), createText: text => node('#text', text), createComment: text => node('#comment', text),
    setText: (el, text) => { el.text = text },
    setElementText: (el, text) => { el.text = text; el.children = [] },
    patchProp: (el, key, _, value) => { el.props[key] = value },
    insert(el, parent, anchor = null) {
      if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1)
      const index = anchor ? parent.children.indexOf(anchor) : parent.children.length
      parent.children.splice(index, 0, el)
      el.parent = parent
    },
    remove(el) { el.parent.children.splice(el.parent.children.indexOf(el), 1) },
    parentNode: el => el.parent,
    nextSibling: el => el.parent?.children[el.parent.children.indexOf(el) + 1],
  })
  const root = node('root')
  const app = renderer.createApp({ render: () => Vue.h(component, props, slots) })
  app.mount(root)
  return { root, unmount: () => app.unmount() }
}

export const findAll = (root, predicate) => [root, ...root.children.flatMap(child => findAll(child, () => true))].filter(predicate)
export const textOf = root => root.tag === '#comment' ? '' : root.text + root.children.map(textOf).join('')

export function click(node) {
  const event = { stopped: false, stopPropagation() { this.stopped = true } }
  for (let current = node; current && !event.stopped; current = current.parent) current.props.onClick?.(event)
}
