import { buildClient } from "./helpers";
import * as helperModule from "./helpers";

export function run(id) {
    const client = new helperModule.JsClient();
    buildClient();
    return client.load(id);
}
