"""Checked native DAV adaptations, applied only to a pinned isolated copy."""
from pathlib import Path
import difflib
import hashlib
import json
import re
import shutil
import textwrap


def prepare_dav(source, destination, specification):
    spec=json.loads(specification.read_text(encoding='utf-8'))
    for name,expected in spec['base_sha256'].items():
        if hashlib.sha256((source/name).read_bytes()).hexdigest()!=expected:
            raise RuntimeError('Pinned DAV input changed: '+name)
    originals={name:(source/name).read_text(encoding='utf-8') for name in spec['base_sha256']}
    changed=dict(originals)

    def replace(file,old,new,count=1):
        if changed[file].count(old)!=count:
            raise RuntimeError('Nonunique DAV patch anchor: '+file+': '+old[:90])
        changed[file]=changed[file].replace(old,new)

    def body(file,name,replacement):
        text=changed[file]
        pattern=r'\b'+re.escape(name)+r'\([^{};]*\)\s*\{'
        matches=list(re.finditer(pattern,text))
        if len(matches)!=1:raise RuntimeError('Nonunique DAV function: '+name)
        start=matches[0].end()-1
        # Mask comments and literals before matching braces; preserve offsets.
        masked=re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
                      lambda m:' '*len(m[0]),text,flags=re.S)
        depth=0;end=None
        for index in range(start,len(masked)):
            if masked[index]=='{':depth+=1
            elif masked[index]=='}':
                depth-=1
                if depth==0:end=index+1;break
        if end is None:raise RuntimeError('Unclosed DAV function: '+name)
        changed[file]=text[:start]+'{\n'+textwrap.dedent(replacement).strip()+'\n}'+text[end:]

    module='mod_dav_svn/mod_dav_svn.c';repos='mod_dav_svn/repos.c'
    activity='mod_dav_svn/activity.c';version='mod_dav_svn/version.c'
    post='mod_dav_svn/posts/create_txn.c';authz='mod_authz_svn/mod_authz_svn.c'
    private='mod_dav_svn/dav_svn.h'
    replace(private,'  apr_pool_t *pool;     /* request_rec -> pool */',
        '  apr_pool_t *pool;     /* request_rec -> pool */\n'
        '  pgvn_repository *pgvn_repository;\n  request_rec *pgvn_request;\n  svn_error_t *pgvn_error;')
    replace(private,'#include "svn_error.h"','#include "svn_error.h"\n#include "pgvn.h"')
    for file in (module,repos,version,activity):
        replace(file,'#include "dav_svn.h"','#include "dav_svn.h"\n#include "adapter.h"')
    replace(module,'#include "adapter.h"','#include "adapter.h"\n#include "status.h"')
    replace(post,'#include "../dav_svn.h"','#include "../dav_svn.h"\n#include "adapter.h"')
    # Shared protocol/public C names are translated by forced-include macros,
    # never by replacing XML, SVN headers, !svn paths or capability strings.
    replace(module,'dav_register_provider(pconf, "svn", &provider);','dav_register_provider(pconf, "pgvn", &provider);')
    replace(module,'  ap_hook_pre_config(init_dso, NULL, NULL, APR_HOOK_REALLY_FIRST);',
        '  pgvn_bootstrap_register_hooks(pconf);\n'
        '  pgvn_dav_status_hooks(pconf);\n'
        '  ap_hook_child_init(pgvn_http_child_init, NULL, NULL, APR_HOOK_MIDDLE);\n'
        '  ap_hook_pre_config(init_dso, NULL, NULL, APR_HOOK_REALLY_FIRST);')
    replace(module,'  { NULL }\n};',
        '  AP_INIT_TAKE1("PgVNConfig", pgvn_http_config, NULL, RSRC_CONF, "Global PgVN connection and hook configuration"),\n  { NULL }\n};')
    body(module,'dav_svn__get_fs_path','return "pgvn:catalog";')
    body(module,'dav_svn__get_fs_parent_path','return NULL;')
    replace(module,'if (conf->fs_path || conf->fs_parent_path)',
        'if (dav_get_provider_name(r) && !strcmp(dav_get_provider_name(r),"pgvn"))',2)
    replace(module,'if (!conf->fs_path && !conf->fs_parent_path)',
        'if (!dav_get_provider_name(r) || strcmp(dav_get_provider_name(r),"pgvn"))')
    replace(module,'      fs_path = conf->fs_path;','      fs_path = "catalog";')
    # URI translation precedes authentication and the DAV handler. Preserve
    # repository errors here as well as in the later access checks.
    replace(module,'          return err->status;',
        '          return pgvn_http_error_response(r,err);')
    body(module,'dav_svn_get_repos_path2','''
        pgvn_binding *binding;
        svn_error_t *error=pgvn_http_resolve(&binding,r,r->uri,pool);
        if(error) return pgvn_http_repository_error(error,r);
        *repos_path=apr_pstrcat(pool,"pgvn:",binding->repository_id,NULL);
        return NULL;
    ''')
    for name in ('SVNPath_cmd','SVNParentPath_cmd','SVNActivitiesDB_cmd','SVNHooksEnv_cmd'):
        body(module,name,'return "PgVN repository paths, activities and hooks are supplied by PostgreSQL policy";')
    replace(module,'      conf->path_authz_method = CONF_PATHAUTHZ_OFF;',
        '      return "PgVN path authorization cannot be disabled";')
    # Prefix configuration directives only. Protocol strings remain unchanged.
    changed[module]=re.sub(r'(AP_INIT_\w+\(")SVN',r'\1PgVN',changed[module])
    replace(module,'  ap_add_version_component(p, "SVN/" SVN_VER_NUMBER);',
        '  ap_add_version_component(p, "SVN/" SVN_VER_NUMBER);\n  ap_add_version_component(p, "PgVN/" PGVN_VERSION);\n'
        '  if (!ap_lookup_provider(AUTHZ_SVN__SUBREQ_BYPASS_PROV_GRP,AUTHZ_SVN__SUBREQ_BYPASS_PROV_NAME,AUTHZ_SVN__SUBREQ_BYPASS_PROV_VER)) {\n'
        '    ap_log_error(APLOG_MARK,APLOG_ERR,0,s,"PgVN requires mod_authz_pgvn");return HTTP_INTERNAL_SERVER_ERROR;\n  }')

    # Location is a mount scope, not a repository root. Select the longest
    # matching enabled catalog route, then retain upstream special-URI parsing.
    replace(repos,'  /* one of these is NULL, the other non-NULL. */','''  pgvn_binding *pgvn_binding;
  svn_error_t *pgvn_error=pgvn_http_resolve(&pgvn_binding,r,uri_to_split,pool);
  if(pgvn_error) return pgvn_http_repository_error(pgvn_error,r);
  root_path=pgvn_binding->route_prefix;

  /* one of these is NULL, the other non-NULL. */''')
    replace(repos,'*repos_basename = svn_dirent_basename(root_path, pool);','*repos_basename = pgvn_binding->canonical_name;')
    replace(repos,'  /* This does all the work of interpreting/splitting the request uri. */','''  pgvn_binding *pgvn_binding;
  serr=pgvn_http_resolve(&pgvn_binding,r,r->uri,r->pool);
  if(serr) return pgvn_http_repository_error(serr,r);
  root_path=pgvn_binding->route_prefix;

  /* This does all the work of interpreting/splitting the request uri. */''')
    a=changed[repos].index('  /* Path to activities database */')
    b=changed[repos].index('  /* Remember various bits for later URL construction */',a)
    changed[repos]=changed[repos][:a]+'  repos->activities_db = NULL; /* PostgreSQL catalog only. */\n\n'+changed[repos][b:]
    a=changed[repos].index('  /* Retrieve/cache open repository */')
    b=changed[repos].index('  /* cache the filesystem object */',a)
    changed[repos]=changed[repos][:a]+'''  /* Request-local binding avoids stale authz and session state across
     keep-alive requests; the shared context only owns the SQL control channel. */
  serr=pgvn_http_open(repos,r);
  if(serr) return pgvn_http_repository_error(serr,r);
  serr=svn_repos_remember_client_capabilities(repos->repos,
      capabilities_as_list(repos->client_capabilities,r->pool));
  if(serr) return dav_svn__sanitize_error(serr,"PgVN capabilities unavailable",HTTP_SERVICE_UNAVAILABLE,r);

'''+changed[repos][b:]
    replace(repos,'dav_svn__get_txn(comb->priv.repos,\n                                              comb->priv.root.vtxn_name)',
        'pgvn_http_get_txn(comb->priv.repos, PGVN_DAV_VIRTUAL,\n                                              comb->priv.root.vtxn_name)',2)
    replace(repos,'  /* get the FS transaction, given its name */','''  if ((derr=pgvn_http_pending(comb->priv.repos))) return derr;
  if ((derr=pgvn_http_validate_txn(comb->priv.repos,txn_name,FALSE))) return derr;

  /* get the FS transaction, given its name */''')
    replace(repos,'      /* Open the named transaction. */','''      dav_error *pgvn_error=pgvn_http_pending(comb->priv.repos);
      if(pgvn_error) return pgvn_error;
      pgvn_error=pgvn_http_validate_txn(comb->priv.repos,comb->priv.root.txn_name,TRUE);
      if(pgvn_error) return pgvn_error;
      pgvn_dav_txn_info *pgvn_info;
      svn_error_t *pgvn_serr=pgvn_dav_lookup(&pgvn_info,comb->priv.repos->pgvn_repository,
          PGVN_DAV_NATIVE,comb->priv.root.txn_name,comb->priv.repos->username,pool);
      if(pgvn_serr) return dav_svn__sanitize_error(pgvn_serr,"PgVN transaction unavailable",HTTP_SERVICE_UNAVAILABLE,comb->priv.r);
      if(SVN_IS_VALID_REVNUM(pgvn_info->committed_revision)) { comb->res.exists=TRUE;return NULL; }
      /* Open the named transaction. */''')
    replace(repos,'  if ((err = prep_resource(comb)) != NULL)\n    return err;',
        '  err=prep_resource(comb);\n  { dav_error *pending=pgvn_http_pending(repos);if(pending) return pending; }\n  if(err) return err;')
    replace(repos,'return dav_svn__delete_activity(resource->info->repos,\n                                        resource->info->root.vtxn_name);',
        'return pgvn_http_cancel(resource->info->repos,PGVN_DAV_VIRTUAL,resource->info->root.vtxn_name);')
    a=changed[repos].index('  if (strcmp(src_repos_path, dst_repos_path) != 0)')
    b=changed[repos].index('  if (!serr)',a)
    changed[repos]=changed[repos][:a]+'''  /* Logical database IDs are never filesystem paths to normalize. */
  if (strcmp(src_repos_path,dst_repos_path))
    return dav_svn__new_error_svn(dst->pool,HTTP_BAD_REQUEST,
        SVN_ERR_UNSUPPORTED_FEATURE,0,"Copy source and destination are in different repositories");
  serr=SVN_NO_ERROR;

'''+changed[repos][b:]

    # Replace the whole activity implementation; no filesystem map helpers are
    # left reachable, including obsolete tmp-file fallbacks.
    changed[activity]=originals[activity][:originals[activity].index('#include')]+'''#include "dav_svn.h"
#include "adapter.h"
#include "svn_dav.h"
#include "private/svn_dav_protocol.h"
#include "http_protocol.h"
#include "apr_strings.h"

const char *dav_svn__get_txn(const dav_svn_repos *repos,const char *name)
{ return pgvn_http_get_txn(repos,PGVN_DAV_ACTIVITY,name); }
dav_error *dav_svn__delete_activity(const dav_svn_repos *repos,const char *name)
{ return pgvn_http_cancel(repos,PGVN_DAV_ACTIVITY,name); }
dav_error *dav_svn__abort_txn(const dav_svn_repos *repos,const char *name,apr_pool_t *pool)
{ return pgvn_http_cancel(repos,PGVN_DAV_NATIVE,name); }
dav_error *dav_svn__store_activity(const dav_svn_repos *repos,const char *alias,const char *name)
{
    /* Creation already stores its alias atomically. A successful MERGE needs
       no metadata write: committed revision is derived from pgfs.transactions. */
    if(name && !*name) return NULL;
    const char *bound=pgvn_http_get_txn(repos,PGVN_DAV_ACTIVITY,alias);
    dav_error *pending=pgvn_http_pending(repos);
    if(pending) return pending;
    if(!bound || strcmp(bound,name)) return dav_svn__new_error(repos->pool,HTTP_CONFLICT,0,0,"PgVN activity binding mismatch");
    return NULL;
}
dav_error *dav_svn__create_txn(dav_svn_repos *repos,const char **name,apr_hash_t *props,apr_pool_t *pool)
{
    const char *alias=apr_table_get(repos->pgvn_request->headers_in,SVN_DAV_VTXN_NAME_HEADER);
    return pgvn_http_create_txn(repos,name,props,alias && *alias ? PGVN_DAV_VIRTUAL : PGVN_DAV_NATIVE,alias && *alias ? alias : NULL,pool);
}
'''
    for file in (post,):
        pattern=r'      if \(\(derr  = dav_svn__store_activity\(resource->info->repos,\s+vtxn_name, txn_name\)\)\)\n        return derr;'
        changed[file],count=re.subn(pattern,'      /* Binding was saved atomically by create_txn. */',changed[file])
        if count!=2:raise RuntimeError('POST virtual mapping patch mismatch')
    replace(version,'  err = dav_svn__create_txn(resource->info->repos, &txn_name,\n                            NULL, resource->pool);',
        '  err = pgvn_http_create_txn(resource->info->repos,&txn_name,NULL,PGVN_DAV_ACTIVITY,activity_id,resource->pool);')
    replace(version,'          derr = dav_svn__create_txn(resource->info->repos, &shared_txn_name,\n                                     NULL, resource->info->r->pool);',
        '          derr = pgvn_http_create_txn(resource->info->repos,&shared_txn_name,NULL,PGVN_DAV_ACTIVITY,shared_activity,resource->info->r->pool);')
    body(version,'register_deltification_cleanup','''
        /* PgFS manages representations through its storage backend. Never
           schedule a later directory-based svn_repos_open on a logical ID. */
        (void)repos; (void)revision; (void)pool;
    ''')
    a=changed[version].index('  /* open the transaction that we\'re going to commit. */')
    b=changed[version].index('  /* ### TODO: Figure out if the MERGE response',a)
    changed[version]=changed[version][:a]+'''  serr=pgvn_http_commit(&conflict,&new_rev,source->info->repos,
      source->info->root.txn_name,&txn,pool);

'''+changed[version][b:]
    replace(version,'        dav_svn__delete_activity(source->info->repos,\n                                 source->info->root.vtxn_name);',
        '        { /* Retain the committed virtual binding for receipt recovery. */ }')
    replace(version,'      svn_error_clear(svn_fs_abort_txn(txn, pool));',
        '      if(txn) svn_error_clear(svn_fs_abort_txn(txn, pool));')
    # Database errors in a legacy string-returning lookup must reach HTTP as
    # service errors rather than being silently converted into unknown aliases.
    replace(version,'          if (! shared_txn_name)\n',
        '          if ((derr=pgvn_http_pending(resource->info->repos))) return derr;\n          if (! shared_txn_name)\n')
    replace(version,'  if ((txn_name = dav_svn__get_txn(resource->info->repos,\n                                   parse.activity_id)) == NULL)',
        '  txn_name=dav_svn__get_txn(resource->info->repos,parse.activity_id);\n'
        '  if ((derr=pgvn_http_pending(resource->info->repos))) return derr;\n  if (txn_name == NULL)')
    replace(version,'      shared_txn_name = dav_svn__get_txn(resource->info->repos,\n                                         shared_activity);',
        '      shared_txn_name = dav_svn__get_txn(resource->info->repos,\n                                         shared_activity);\n'
        '      if ((err=pgvn_http_pending(resource->info->repos))) return err;')

    # Keep upstream method/recursive/copy/history checking, but load policy for
    # each repository independently and enforce its protocol access profile.
    replace(authz,'#include "mod_dav_svn.h"','#include "mod_dav_svn.h"\n#include "adapter.h"')
    replace(authz,'static const command_rec authz_svn_cmds[] =',
        'static const char *pgvn_authoritative_cmd(cmd_parms *cmd,void *config,int enabled)\n'
        '{ return enabled ? NULL : "PgVN path authorization must remain authoritative"; }\n\n'
        'static const command_rec authz_svn_cmds[] =')
    replace(authz,'AP_INIT_FLAG("AuthzSVNAuthoritative", ap_set_flag_slot,','AP_INIT_FLAG("AuthzSVNAuthoritative", pgvn_authoritative_cmd,')
    replace(authz,'"AuthzForceUsernameCase"','"AuthzPgVNForceUsernameCase"')
    # With automatic provider-based activation there need not be a directive
    # owned by authz in this Location. Its inherited base_path may be NULL.
    a=changed[authz].index('  if (strcmp(svn_urlpath__canonicalize(r->uri, r->pool), conf->base_path) == 0)')
    b=changed[authz].index('  dav_err = dav_svn_split_uri(r,',a)
    changed[authz]=changed[authz][:a]+changed[authz][b:]
    changed[authz]=changed[authz].replace('conf->base_path,','pgvn_http_mount(r),').replace(
        'strlen(conf->base_path)','strlen(pgvn_http_mount(r))')
    guard='(conf->access_file || conf->repo_relative_access_file)'
    if changed[authz].count(guard)!=5:raise RuntimeError('Authz activation guard count mismatch')
    changed[authz]=changed[authz].replace(guard,'(dav_get_provider_name(r) && !strcmp(dav_get_provider_name(r),"pgvn"))')
    # A catalog error is discovered in the access checker before the DAV
    # handler runs. Keep its sanitized SVN XML instead of a bare HTTP status.
    # The upstream OK/DECLINED guard still makes every error terminal.
    replace(authz,'dav_err->status : HTTP_INTERNAL_SERVER_ERROR;',
        'pgvn_http_error_response(r,dav_err) : HTTP_INTERNAL_SERVER_ERROR;',2)
    body(authz,'get_access_conf','''
        svn_authz_t *policy=NULL;
        svn_error_t *error=pgvn_http_policy(&policy,r,scratch_pool);
        if(error) {
            pgvn_exit_code code=pgvn_error_exit_code(error);
            const char *status=svn_error_find_cause(error,SVN_ERR_AUTHZ_INVALID_CONFIG) ? "403" :
                (code==PGVN_EXIT_NOT_FOUND ? "404" : "503");
            apr_table_setn(r->notes,"pgvn-authz-error-status",status);
            log_svn_error(APLOG_MARK,r,"PgVN authorization policy unavailable:",error,scratch_pool);return NULL;
        }
        return policy;
    ''')
    replace(authz,'  if (access_conf == NULL)\n    return DECLINED;',
        '  if (access_conf == NULL) {\n    const char *status=apr_table_get(r->notes,"pgvn-authz-error-status");\n'
        '    return status ? (int)apr_atoi64(status) : HTTP_SERVICE_UNAVAILABLE;\n  }')
    replace(authz,'  conf->anonymous = 1;','  conf->anonymous = 1;\n  conf->access_file = "pgvn:catalog";')
    for name in ('AuthzSVNAccessFile_cmd','AuthzSVNReposRelativeAccessFile_cmd','AuthzSVNGroupsFile_cmd'):
        body(authz,name,'return "PgVN authorization is loaded from versioned PostgreSQL policy";')
    changed[authz],count=re.subn(r'svn_repos_authz_check_access\(access_conf,\s*',
        'pgvn_http_check_access(r, ',changed[authz])
    if count!=3:raise RuntimeError('Authz access adapter count mismatch')
    replace(authz,'  static const char * const mod_ssl[] = { "mod_ssl.c", NULL };',
        '  static const char * const mod_ssl[] = { "mod_ssl.c", NULL };\n  pgvn_bootstrap_register_hooks(p);')
    changed[authz]=re.sub(r'(AP_INIT_\w+\(")AuthzSVN',r'\1AuthzPgVN',changed[authz])
    # Private bypass provider names must not resolve to stock mod_authz_svn.
    for key in ('include/mod_authz_svn.h',):
        changed[key]=changed[key].replace('"dav2authz_subreq_bypass"','"pgvn2authz_subreq_bypass"').replace(
            '"mod_authz_svn_subreq_bypass"','"mod_authz_pgvn_subreq_bypass"')

    for name in changed:
        for old,new in (("SVN-MERGE","PGVN-MERGE"),("LocationRewrite","PgVNLocationRewrite"),
                        ("ReposRewrite","PgVNReposRewrite"),("IncomingRewrite","PgVNIncomingRewrite"),
                        ("svn-status","pgvn-status"),("dav_svn-no-map-to-storage","pgvn-no-map-to-storage")):
            changed[name]=changed[name].replace('"'+old+'"','"'+new+'"')

    destination.mkdir(parents=True,exist_ok=True)
    diff=[]
    for name,text in changed.items():
        path=destination/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(text,encoding='utf-8')
        if text!=originals[name]:diff.extend(difflib.unified_diff(originals[name].splitlines(True),text.splitlines(True),fromfile='a/'+name,tofile='b/'+name))
    # C-only macro namespacing, including Apache module data and public API.
    identifiers=set(re.findall(r'\b(?:dav_svn|authz_svn)\w*\b','\n'.join(changed.values())))
    header='/* Generated from pinned sources; C identifiers only. */\n'
    for name in sorted(identifiers):
        header+='#define '+name+' '+name.replace('dav_svn','dav_pgvn',1).replace('authz_svn','authz_pgvn',1)+'\n'
    (destination/'pgvn_dav_namespace.h').write_text(header,encoding='ascii')
    (destination/'applied-dav.patch').write_text(''.join(diff),encoding='utf-8')
    return {name:hashlib.sha256((destination/name).read_bytes()).hexdigest() for name in changed}
