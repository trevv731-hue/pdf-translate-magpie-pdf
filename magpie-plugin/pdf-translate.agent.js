// PDF Translate as a magpie agent.
//
// The translator keeps its engine settings in config.env beside the app:
// ENGINE, MODEL_SOURCE, API_KEY, MODEL, BASE_URL, QPS. With this plugin the
// row for it shows up on magpie's Agents page, and picking one of magpie's
// models there writes that model, the gateway's address and a key of its own
// into config.env. The translator then runs on magpie's gateway, so the
// provider, the account and the failover are all chosen in magpie.
//
// MODEL_SOURCE=pinned tells the translator to use the MODEL written here as
// it stands, rather than following whichever model some other agent happens
// to be on (MODEL_SOURCE=magpie, the older behaviour).
//
// Picking the agent's own model again, or switching the row off, puts
// config.env back as it was: magpie restores what each key held and takes
// out the ones it added. QPS and the comments are left alone either way.
export const agent = {
  id: "pdf-translate",
  name: "PDF Translate",
  aliases: ["pdftranslate", "pdfq"],
  // the app's own folder, which is also what says it is installed
  dir: "D:/Tools/PDF翻译工具-便携版",
  config: "D:/Tools/PDF翻译工具-便携版/config.env",
  format: "env",
  model: "MODEL",
  notice:
    "PDF Translate reads config.env when a translation starts: open it again (or drag the next PDF in) to use the new model.",
}

// The keys the translator needs beside the model. ENGINE picks the
// OpenAI-compatible path, MODEL_SOURCE=pinned keeps the model pinned to
// what magpie wrote, and the gateway's address and key are magpie's own.
export function connect({ gateway }) {
  return {
    ENGINE: "openaicompatible",
    MODEL_SOURCE: "pinned",
    BASE_URL: gateway.v1,
    API_KEY: gateway.key,
  }
}
