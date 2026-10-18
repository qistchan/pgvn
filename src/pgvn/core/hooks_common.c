#include <stdio.h>
#include <string.h>
#include "internal.h"
#include <apr_base64.h>
#include <apr_strings.h>
#include "private/svn_utf_private.h"

#define HOOK_JSON_LIMIT (32u*1024u*1024u)

static const char *events[]={"start-commit","pre-commit","post-commit","pre-revprop-change",
    "post-revprop-change","pre-lock","post-lock","pre-unlock","post-unlock"};

const char *pgvn_hook_name(pgvn_hook_event event)
{
    return event>=0 && event<=PGVN_HOOK_POST_UNLOCK ? events[event] : "unknown";
}

static svn_error_t *event_limit(void)
{
    return svn_error_create(SVN_ERR_REPOS_HOOK_FAILURE,NULL,"PgVN hook event exceeds its resource limit");
}

static svn_error_t *json_string(svn_stringbuf_t *buffer,const char *value)
{
    if(!value) { svn_stringbuf_appendcstr(buffer,"null"); return SVN_NO_ERROR; }
    size_t size=strlen(value);
    if(size>HOOK_JSON_LIMIT/6 || buffer->len>HOOK_JSON_LIMIT-6*size-2) return event_limit();
    svn_stringbuf_appendbyte(buffer,'"');
    for(const unsigned char *p=(const unsigned char *)value;*p;++p) {
        if(*p=='"' || *p=='\\') { svn_stringbuf_appendbyte(buffer,'\\'); svn_stringbuf_appendbyte(buffer,*p); }
        else if(*p<32) { char escape[7]; snprintf(escape,sizeof(escape),"\\u%04x",*p); svn_stringbuf_appendcstr(buffer,escape); }
        else svn_stringbuf_appendbyte(buffer,*p);
    }
    svn_stringbuf_appendbyte(buffer,'"');
    return SVN_NO_ERROR;
}

static svn_error_t *json_field(svn_stringbuf_t *buffer,const char *name,const char *value)
{
    svn_stringbuf_appendcstr(buffer,",\"");
    svn_stringbuf_appendcstr(buffer,name);
    svn_stringbuf_appendcstr(buffer,"\":");
    return json_string(buffer,value);
}

static svn_error_t *json_array(svn_stringbuf_t *buffer,const char *name,const apr_array_header_t *values)
{
    svn_stringbuf_appendcstr(buffer,",\"");
    svn_stringbuf_appendcstr(buffer,name);
    svn_stringbuf_appendcstr(buffer,"\":[");
    for(int i=0;values && i<values->nelts;++i) {
        if(i) svn_stringbuf_appendbyte(buffer,',');
        SVN_ERR(json_string(buffer,APR_ARRAY_IDX(values,i,const char *)));
    }
    svn_stringbuf_appendbyte(buffer,']');
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_hook_event_json(svn_stringbuf_t **output,const pgvn_hook_context *event,apr_pool_t *pool)
{
    svn_stringbuf_t *buffer=svn_stringbuf_create("{\"version\":1",pool);
    SVN_ERR(json_field(buffer,"event",pgvn_hook_name(event->event)));
    SVN_ERR(json_field(buffer,"repository_id",event->repository_id));
    SVN_ERR(json_field(buffer,"username",event->username));
    SVN_ERR(json_field(buffer,"transaction_id",event->transaction_id));
    svn_stringbuf_appendcstr(buffer,apr_psprintf(pool,",\"revision\":%ld,\"force\":%s",event->revision,event->force ? "true" : "false"));
    SVN_ERR(json_field(buffer,"path",event->path));
    SVN_ERR(json_field(buffer,"property_name",event->property_name));
    char action[2]={event->action,0};
    SVN_ERR(json_field(buffer,"action",event->action ? action : NULL));
    SVN_ERR(json_field(buffer,"lock_token",event->lock_token));
    SVN_ERR(json_field(buffer,"lock_comment",event->lock_comment));
    SVN_ERR(json_array(buffer,"paths",event->paths));
    SVN_ERR(json_array(buffer,"capabilities",event->capabilities));
    if(event->property_value) {
        if(event->property_value->len>16u*1024u*1024u) return event_limit();
        int size=apr_base64_encode_len((int)event->property_value->len);
        if(buffer->len+(apr_size_t)size+32>HOOK_JSON_LIMIT) return event_limit();
        char *encoded=apr_palloc(pool,(apr_size_t)size);
        apr_base64_encode_binary(encoded,(const unsigned char *)event->property_value->data,(int)event->property_value->len);
        svn_stringbuf_appendcstr(buffer,",\"property_value_base64\":\"");
        svn_stringbuf_appendcstr(buffer,encoded);
        svn_stringbuf_appendbyte(buffer,'"');
    } else SVN_ERR(json_field(buffer,"property_value_base64",NULL));
    svn_stringbuf_appendcstr(buffer,"}\n");
    if(buffer->len>HOOK_JSON_LIMIT) return event_limit();
    *output=buffer;
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_hook_result(const pgvn_hook_context *event,
    const char *stdout_data,size_t stdout_size,svn_boolean_t stdout_truncated,
    const char *stderr_data,size_t stderr_size,unsigned long exit_code,apr_pool_t *pool)
{
    if(exit_code) {
        const char *message=stderr_data;
        if(strlen(message)!=stderr_size || !svn_utf__is_valid(message,stderr_size)) message="(non-text output omitted)";
        const char *name=pgvn_hook_name(event->event),*action=NULL;
        if(event->event==PGVN_HOOK_START_COMMIT || event->event==PGVN_HOOK_PRE_COMMIT) action="Commit";
        else if(event->event==PGVN_HOOK_PRE_REVPROP) action="Revprop change";
        else if(event->event==PGVN_HOOK_PRE_LOCK) action="Lock";
        else if(event->event==PGVN_HOOK_PRE_UNLOCK) action="Unlock";
        const char *description=action ?
            apr_psprintf(pool,"%s blocked by %s hook (exit code %d)",action,name,(int)exit_code) :
            apr_psprintf(pool,"%s hook failed (exit code %d)",name,(int)exit_code);
        return svn_error_createf(SVN_ERR_REPOS_HOOK_FAILURE,NULL,"%s%s%s",description,
            *message ? " with output:\n" : " with no output.",message);
    }
    if(event->lock_token_result) {
        size_t start=0,end=stdout_size;
        while(start<end && strchr(" \t\r\n",stdout_data[start])) ++start;
        while(end>start && strchr(" \t\r\n",stdout_data[end-1])) --end;
        if(stdout_truncated || end-start>4096 || memchr(stdout_data,0,stdout_size) ||
            memchr(stdout_data+start,'\n',end-start) || memchr(stdout_data+start,'\r',end-start) ||
            !svn_utf__is_valid(stdout_data+start,end-start))
            return svn_error_create(SVN_ERR_REPOS_HOOK_FAILURE,NULL,"PgVN pre-lock hook returned an invalid token");
        *event->lock_token_result=apr_pstrmemdup(pool,stdout_data+start,end-start);
    }
    return SVN_NO_ERROR;
}
