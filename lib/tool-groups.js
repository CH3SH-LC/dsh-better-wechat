// Tool ownership for the split Cordis components.
//
// `lib/tools.js` stays the single source of truth for tool names, parameter
// schemas, descriptions and validation. This module only derives *which*
// component owns which tool, by the action that `TOOL_SPECS` already declares.
// Deriving instead of re-listing means a renamed or added spec can never be
// silently double-registered or forgotten by a split component.
import { TOOL_SPECS } from './tools.js';

/** Human-readable functions per split component, keyed by the component file. */
export const TOOL_GROUPS = Object.freeze({
  'components/core.js': Object.freeze([]),
  'components/stage-one.js': Object.freeze(['prepare', 'refresh']),
  'components/read.js': Object.freeze(['status', 'search', 'context', 'review', 'conversations']),
  'components/tasks.js': Object.freeze(['save_tasks', 'list_tasks']),
  'components/preferences.js': Object.freeze(['get_preferences', 'set_preferences']),
});

/** Same ownership expressed as tool names, in `TOOL_SPECS` order. */
export const GROUP_TOOL_NAMES = Object.freeze(Object.fromEntries(
  Object.entries(TOOL_GROUPS).map(([component, actions]) => [component, Object.freeze(toolNames(actions))])));

/** Every tool name declared by `TOOL_SPECS`, in declaration order. */
export const DECLARED_TOOL_NAMES = Object.freeze(TOOL_SPECS.map(spec => spec.name));

function toolNames(actions) {
  const wanted = new Set(actions);
  return TOOL_SPECS.filter(spec => wanted.has(spec.action)).map(spec => spec.name);
}

/**
 * Return the exact subset of `TOOL_SPECS` a component owns, in declaration
 * order. An unknown component or unknown action is a wiring error, never an
 * empty group: a split component that silently owned no tool would report a
 * healthy start while its tools disappeared from the model.
 */
export function toolSpecsFor(component) {
  const actions = TOOL_GROUPS[component];
  if (!actions) throw new Error(`unknown tool group: ${component}`);
  const wanted = new Set(actions);
  const known = new Set(TOOL_SPECS.map(spec => spec.action));
  for (const action of actions) if (!known.has(action)) throw new Error(`unknown tool action: ${action}`);
  const specs = TOOL_SPECS.filter(spec => wanted.has(spec.action));
  if (specs.length !== actions.length) throw new Error(`duplicate tool action in ${component}`);
  return specs;
}

/** Throw unless every declared tool is owned by exactly one component. */
export function assertCompleteToolPartition(specs = TOOL_SPECS) {
  const declared = specs.map(spec => spec.name);
  const owned = [];
  for (const component of Object.keys(TOOL_GROUPS)) owned.push(...toolNames(TOOL_GROUPS[component]));
  const duplicates = owned.filter((name, index) => owned.indexOf(name) !== index);
  if (duplicates.length) throw new Error(`duplicate tool ownership: ${duplicates.join(', ')}`);
  const missing = declared.filter(name => !owned.includes(name));
  const extra = owned.filter(name => !declared.includes(name));
  if (missing.length || extra.length) {
    throw new Error(`tool partition mismatch: missing=[${missing}] extra=[${extra}]`);
  }
  return true;
}
