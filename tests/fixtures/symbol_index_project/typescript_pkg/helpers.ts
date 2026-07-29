export interface User {
    id: string;
}

export function fetchUser(id: string): User {
    return { id };
}

export const normalizeUser = (id: string): string => {
    return id.trim();
};

export default class Client {
    load(id: string): User {
        return fetchUser(id);
    }
}
