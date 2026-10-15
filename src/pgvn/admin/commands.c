#include <string.h>
#include "admin.h"
#define O(x) OPTION(OPT_##x)
typedef struct command_definition {
    const char *group;
    const char *verb;
    int minimum;
    int maximum;
    uint64_t options;
} command_definition;
static const command_definition commands[]={
    {"db","init",2,2,0},{"db","migrate",2,2,0},{"db","check",2,2,0},
    {"db","grant",2,2,O(ADMIN_ROLE)|O(SERVICE_ROLE)|O(MONITOR_ROLE)},
    {"repo","create",3,3,O(ROUTE)|O(UUID)},{"repo","list",2,2,O(LIMIT)|O(OFFSET)},
    {"repo","show",3,3,0},{"repo","register-existing",4,4,O(ROUTE)},
    {"repo","enable",3,3,0},{"repo","disable",3,3,0},
    {"dump",NULL,2,2,O(FILE)|O(REVISION)|O(INCREMENTAL)|O(DELTAS)},
    {"load",NULL,2,2,O(FILE)|O(REVISION)|O(FORCE_UUID)|O(IGNORE_UUID)|O(USE_HOOKS)},
    {"verify",NULL,2,2,O(REVISION)},
    {"look","youngest",3,3,0},{"look","uuid",3,3,0},{"look","info",3,3,O(REVISION)|O(TXN)},
    {"look","tree",3,4,O(REVISION)|O(TXN)|O(FULL_PATHS)},
    {"look","cat",4,4,O(REVISION)|O(TXN)}, {"look","changed",3,3,O(REVISION)|O(TXN)},
    {"look","proplist",3,4,O(REVISION)|O(TXN)|O(REVPROP)|O(VERBOSE)},
    {"look","propget",4,5,O(REVISION)|O(TXN)|O(REVPROP)},
    {"look","transactions",3,3,0},{"look","txn-info",3,3,O(TXN)},
    {"maintenance","pack",3,3,0},{"maintenance","recover",3,3,0},
    {"maintenance","freeze",3,3,O(TIMEOUT)},
    {"maintenance","hotcopy",4,4,O(INCREMENTAL)|O(ROUTE)},
    {"maintenance","purge",3,3,O(CONFIRM_ID)},{"maintenance","abort-txn",4,4,0},
    {"config","show",3,3,0},{"config","publish",3,3,O(GENERATION)|O(AUTHZ)|O(GROUPS)|O(HOOKS)|O(SETTINGS)},
    {"authz","validate",2,2,O(AUTHZ)|O(GROUPS)}, {"authz","check",6,6,0},
    {"monitor-token","create",2,2,O(LABEL)|O(EXPIRES)}, {"monitor-token","list",2,2,0},
    {"monitor-token","revoke",3,3,0}, {"monitor-token","expire",3,3,O(EXPIRES)},
    {"key","create",4,4,0}, {"user","set",4,4,O(KEY_FILE)},
    {"user","rekey",4,4,O(KEY_FILE)|O(OLD_KEY_FILE)},
    {"user","list",2,3,0}, {"user","disable",4,4,0},
    {"diagnose",NULL,1,1,0}
};

svn_error_t *admin_preflight(admin_command *command)
{
    if(!command->argc) return admin_usage("Command is required; see pgvnadmin --help");
    const command_definition *definition=NULL;
    for(apr_size_t i=0;i<sizeof(commands)/sizeof(commands[0]);++i)
        if(!strcmp(command->argv[0],commands[i].group) && (!commands[i].verb ||
           (command->argc>1 && !strcmp(command->argv[1],commands[i].verb)))) { definition=&commands[i]; break; }
    if(!definition) return admin_usage("Unknown command; see pgvnadmin --help");
    SVN_ERR(admin_options(command,definition->options,definition->minimum,definition->maximum));
    if(command->values[OPT_TXN] && command->values[OPT_REVISION]) return admin_usage("Choose either a revision or a transaction");
    if((command->flags & O(FORCE_UUID)) && (command->flags & O(IGNORE_UUID))) return admin_usage("UUID options are mutually exclusive");
    if(!strcmp(definition->group,"dump") && (command->flags & O(JSON))) return admin_usage("A dump is a binary SVN stream; --json is not accepted");
    if(!strcmp(definition->group,"maintenance") && !strcmp(definition->verb,"freeze") && !command->child_argc)
        return admin_usage("Freeze requires -- PROGRAM ARGS");
    if(!strcmp(definition->group,"maintenance") && !strcmp(definition->verb,"freeze") && (command->flags & O(JSON)))
        return admin_usage("Freeze preserves child standard streams; --json is not accepted");
    if(!strcmp(definition->group,"maintenance") && !strcmp(definition->verb,"purge") && !command->values[OPT_CONFIRM_ID])
        return admin_usage("Purge requires --confirm-id with the exact storage ID");
    if((!strcmp(definition->group,"authz") && !strcmp(definition->verb,"validate")) ||
       (!strcmp(definition->group,"config") && !strcmp(definition->verb,"publish"))) {
        if(!command->values[OPT_AUTHZ]) return admin_usage("--authz FILE is required");
        if(!strcmp(definition->group,"config") && !command->values[OPT_GENERATION]) return admin_usage("--expected-generation is required for configuration publication");
    }
    if(!strcmp(definition->group,"monitor-token") && !strcmp(definition->verb,"create") && !command->values[OPT_LABEL]) return admin_usage("A monitor token requires --label");
    if(!strcmp(definition->group,"monitor-token") && !strcmp(definition->verb,"expire") && !command->values[OPT_EXPIRES]) return admin_usage("Token expiry requires --expires-in SECONDS");
    if(!strcmp(definition->group,"db") && !strcmp(definition->verb,"grant") &&
       !(command->flags & (O(ADMIN_ROLE)|O(SERVICE_ROLE)|O(MONITOR_ROLE)))) return admin_usage("Specify at least one role to grant");
    return SVN_NO_ERROR;
}

svn_error_t *admin_dispatch(admin_command *command)
{
    const char *group=command->argv[0];
    if(!strcmp(group,"db")) return admin_db(command);
    if(!strcmp(group,"repo")) return admin_repo(command);
    if(!strcmp(group,"look") || !strcmp(group,"dump") || !strcmp(group,"load") || !strcmp(group,"verify")) return admin_history(command);
    if(!strcmp(group,"config") || !strcmp(group,"authz")) return admin_policy(command);
    if(!strcmp(group,"maintenance")) return admin_maintenance(command);
    if(!strcmp(group,"monitor-token")) return admin_tokens(command);
    if(!strcmp(group,"key") || !strcmp(group,"user")) return admin_credentials(command);
    return admin_diagnose(command);
}
