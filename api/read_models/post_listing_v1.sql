CREATE TABLE public.post_listing (
 post_id integer NOT NULL,
 safety varchar(32),
 file_size bigint,
 PRIMARY KEY(post_id) INCLUDE(file_size)
);
-- statement-break
LOCK TABLE public.post IN SHARE MODE;
-- statement-break
INSERT INTO public.post_listing(post_id,safety,file_size)
 SELECT id,safety,file_size FROM public.post WHERE status='ok';
-- statement-break
ALTER TABLE public.post_listing ADD CONSTRAINT post_listing_post_id_fkey
 FOREIGN KEY(post_id) REFERENCES public.post(id) ON DELETE CASCADE ON UPDATE CASCADE;
-- statement-break
CREATE INDEX ix_post_listing_safe ON public.post_listing(post_id) INCLUDE(file_size) WHERE safety='safe';
-- statement-break
CREATE FUNCTION public.sync_post_listing() RETURNS trigger LANGUAGE plpgsql
 SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 IF TG_RELID<>'public.post'::regclass THEN
  RAISE EXCEPTION 'Listing projection accepts only canonical post writes';
 END IF;
 IF NEW.status='ok' THEN
  INSERT INTO public.post_listing(post_id,safety,file_size) VALUES(NEW.id,NEW.safety,NEW.file_size)
  ON CONFLICT(post_id) DO UPDATE SET safety=EXCLUDED.safety,file_size=EXCLUDED.file_size
  WHERE (post_listing.safety,post_listing.file_size) IS DISTINCT FROM (EXCLUDED.safety,EXCLUDED.file_size);
 ELSE
  DELETE FROM public.post_listing WHERE post_id=NEW.id;
 END IF;
 RETURN NEW;
END $$;
-- statement-break
CREATE TRIGGER post_listing_insert AFTER INSERT ON public.post
 FOR EACH ROW EXECUTE FUNCTION public.sync_post_listing();
-- statement-break
CREATE TRIGGER post_listing_update AFTER UPDATE OF id,status,safety,file_size ON public.post
 FOR EACH ROW WHEN ((OLD.id,OLD.status,OLD.safety,OLD.file_size) IS DISTINCT FROM (NEW.id,NEW.status,NEW.safety,NEW.file_size))
 EXECUTE FUNCTION public.sync_post_listing();
