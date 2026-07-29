import Client, { fetchUser as fetchAccount, normalizeUser } from "./helpers";
import * as helpers from "./helpers";

export class Service {
    run(id: string) {
        const client = new Client();
        fetchAccount(id);
        normalizeUser(id);
        helpers.fetchUser(id);
        return client.load(id);
    }
}
